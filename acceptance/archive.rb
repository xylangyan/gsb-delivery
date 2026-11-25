# 回放档案模型与夹具定义。
#
# 档案要素（对应验收登记）：
#   t0          默认时钟起点
#   templates   标准动作片；每片 = 固定步骤序列 + 重复轮次（压缩记录，
#               长验收单片可重复上亿次，不展开存储）
#   step.dt     每步应推进的时间（秒），执行步骤前先推进时钟
#   probe_slots 允许插入判空探针的位置：slot i 表示“第 i 步之前”
#               （slot == steps.length 表示本轮最后一步之后、轮边界上）
#
# 卡点（checkpoint）即步骤本身：
#   recv  模拟收件（jar.add）
#   del   删除（jar.delete）
#   gc    清理（jar.cleanup）
#   read  按路径读（jar.each(uri)，记录交出顺序与访问时刻）
#   round 再开一轮（轮边界签收：jar.map 全量有序盘点，顺带清过期）
module Acceptance
  Step = Struct.new(:dt, :op, :args, keyword_init: true) do
    def label
      case op
      when :recv  then "recv(#{args[:name]}@#{args[:domain]}#{args[:path]},max_age=#{args[:max_age] || 'session'})"
      when :del   then "del(#{args[:name]}@#{args[:domain]}#{args[:path]})"
      when :gc    then 'gc'
      when :read  then "read(#{args[:uri]})"
      when :round then 'round(签收)'
      end
    end
  end

  Template = Struct.new(:id, :steps, :repeats, :probe_slots, keyword_init: true)
  Archive  = Struct.new(:name, :t0, :templates, keyword_init: true)

  module Fixtures
    SVC  = 'svc.test.example'
    API  = 'api.test.example'
    BETA = 'beta.test.example'
    T0   = 1_767_225_600 # 2026-01-01 00:00:00 UTC，登记的默认时钟

    def self.recv(name, domain, path, max_age, dt: 60)
      Step.new(dt: dt, op: :recv, args: {
        name: name, domain: domain, path: path,
        value: "v-#{name.downcase}", max_age: max_age,
      })
    end

    def self.del(name, domain, path, dt: 60)
      Step.new(dt: dt, op: :del, args: { name: name, domain: domain, path: path })
    end

    def self.read(uri, dt: 60)
      Step.new(dt: dt, op: :read, args: { uri: uri })
    end

    def self.gc(dt: 60)
      Step.new(dt: dt, op: :gc, args: {})
    end

    def self.round(dt: 60)
      Step.new(dt: dt, op: :round, args: {})
    end

    # 小夹具：3 轮，直接用原库逐候选各跑一遍核结果。
    # 复现登记场景：某路径先登记 A 再登记 B；探针时刻 A 过期 B 有效，
    # empty? 清掉 A 后碰到 B 即停；稍后同键重录 A，落位从“覆盖旧位”
    # 变成“追加到 B 后”，后续 read 的交出顺序随之分歧。
    def self.small
      steps = [
        recv('A', SVC, '/p', 150),  # 0
        recv('B', SVC, '/p', 900),  # 1
        read("http://#{SVC}/p"),    # 2
        recv('X', API, '/q', 30),   # 3
        recv('A', SVC, '/p', 150),  # 4 同键重录 A
        read("http://#{SVC}/p"),    # 5
        del('X', API, '/q'),        # 6
        gc,                         # 7
        round,                      # 8
      ]
      Archive.new(name: 'small', t0: Time.at(T0), templates: [
        Template.new(id: 'small_loop', steps: steps, repeats: 3,
                     probe_slots: (0..steps.length).to_a),
      ])
    end

    # 长验收：单片压缩重复 1.2 亿轮。结构上与 small 同族，另含
    # “清理提前做了同样的删除、分歧在签收点前消失”的可插情形（slot 7）。
    def self.long
      steps = [
        recv('N', SVC, '/m', 90),    # 0
        recv('P', SVC, '/m', 1000),  # 1 长寿命，跨轮留存
        recv('O', API, '/o', 500),   # 2
        recv('Q', BETA, '/b', 30),   # 3
        recv('N', SVC, '/m', 90),    # 4 同键重录 N
        read("http://#{SVC}/m"),     # 5
        del('Q', BETA, '/b'),        # 6
        gc,                          # 7
        round,                       # 8
      ]
      Archive.new(name: 'long', t0: Time.at(T0), templates: [
        Template.new(id: 'long_loop', steps: steps, repeats: 120_000_000,
                     probe_slots: (0..steps.length).to_a),
      ])
    end
  end
end
