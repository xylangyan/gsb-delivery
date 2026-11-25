# 固定时钟：回放期间把 Time.now 钉在登记的时钟上，按步推进。
# 库（HashStore#each / #cleanup、Cookie#expired?、created_at / accessed_at）
# 全部走 Time.now，钉住它即可让整个回放完全确定。
module FakeClock
  @time = nil
  @installed = false

  class << self
    attr_accessor :time

    def install!
      return if @installed
      Time.singleton_class.prepend(Module.new do
        def now
          FakeClock.time or super
        end
      end)
      @installed = true
    end
  end
end
