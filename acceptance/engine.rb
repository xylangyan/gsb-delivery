# 执行引擎：驱动原库 HashStore 跑回放，提供快照 / 归一化指纹 / 探针。
module Acceptance
  class Engine
    attr_reader :jar

    def initialize
      @jar = HTTP::CookieJar.new # 默认即 HashStore
    end

    def snapshot
      store = @jar.store
      Marshal.dump([
        store.instance_variable_get(:@jar),
        store.instance_variable_get(:@gc_index),
        FakeClock.time,
      ])
    end

    def restore(snap)
      jar_hash, gc_index, time = Marshal.load(snap)
      store = @jar.store
      store.instance_variable_set(:@jar, jar_hash)
      store.instance_variable_set(:@gc_index, gc_index)
      FakeClock.time = time
      self
    end

    # 时间归一化指纹：所有时刻减去当前时钟，使同一相对状态在不同
    # 轮次得到同一指纹，用于周期检测（瞬态 μ + 周期 λ）。
    def fingerprint
      now = FakeClock.time
      store = @jar.store
      data = store.instance_variable_get(:@jar).map { |domain, paths|
        [domain, paths.map { |path, hash|
          [path, hash.map { |name, c|
            [name, c.value,
             c.expires && (c.expires - now).to_i,
             (c.created_at - now).to_i,
             (c.accessed_at - now).to_i,
             c.for_domain?]
          }]
        }]
      }
      Marshal.dump([data, store.instance_variable_get(:@gc_index)])
    end

    # 执行一步并返回卡点记录 [kind, payload]。
    # read 记录交出的 [name, domain, path, accessed_at] 有序序列；
    # round 签收记录全量有序盘点。
    def exec_step(step)
      FakeClock.time += step.dt
      case step.op
      when :recv
        a = step.args
        opts = { name: a[:name], value: a[:value], domain: a[:domain], path: a[:path] }
        opts[:expires] = FakeClock.time + a[:max_age] if a[:max_age]
        @jar.add(HTTP::Cookie.new(opts))
        [:recv, a[:name]]
      when :del
        a = step.args
        @jar.delete(HTTP::Cookie.new(name: a[:name], value: '', domain: a[:domain], path: a[:path]))
        [:del, a[:name]]
      when :gc
        @jar.cleanup
        [:gc, nil]
      when :read
        out = []
        @jar.each(URI(step.args[:uri])) { |c| out << [c.name, c.domain, c.path, c.accessed_at.to_i] }
        [:read, out]
      when :round
        inv = @jar.map { |c| [c.name, c.domain, c.path] }
        [:round, inv]
      end
    end

    # 判空探针：CookieJar#empty?（无参，走 HashStore 的 each 早停）。
    # 结果另行记录；返回探出的过期清除清单供报告使用。
    def probe_empty
      before = raw_entries
      result = @jar.empty?
      { result: result, purged: before - raw_entries }
    end

    def raw_entries
      store = @jar.store
      entries = []
      store.instance_variable_get(:@jar).each { |domain, paths|
        paths.each { |path, hash| hash.each_key { |name| entries << "#{name}@#{domain}#{path}" } }
      }
      entries
    end
  end
end
