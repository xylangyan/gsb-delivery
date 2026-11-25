# frozen_string_literal: true

require "monitor"

module ArchiveHandoff
  class Progress
    class Subscription
      attr_reader :id

      def initialize(bus, id, block)
        @bus = bus
        @id = id
        @block = block
        @queue = Queue.new
        @lock = Monitor.new
        @condition = @lock.new_cond
        @paused = false
        @closed = false
        @consumer = Thread.new { consume }
      end

      def pause
        @lock.synchronize do
          @paused = true
        end
        self
      end

      def resume
        @lock.synchronize do
          @paused = false
          @condition.broadcast
        end
        self
      end

      def paused?
        @lock.synchronize { @paused }
      end

      def push(event)
        return if @closed

        @queue << event
      end

      def close
        return if @closed

        @lock.synchronize do
          @closed = true
          @condition.broadcast
        end
        @queue << :stop
        @consumer.join
        @bus.unsubscribe(self)
      end

      private

      def consume
        while (event = @queue.pop)
          break if event == :stop

          @lock.synchronize do
            @condition.wait_while { @paused && !@closed }
          end
          break if @closed

          @block.call(event)
        end
      rescue StandardError
      end
    end

    def initialize
      @subscriptions = []
      @mutex = Mutex.new
      @next_id = 1
    end

    def subscribe(&block)
      @mutex.synchronize do
        subscription = Subscription.new(self, @next_id, block)
        @next_id += 1
        @subscriptions << subscription
        subscription
      end
    end

    def publish(event)
      @mutex.synchronize { @subscriptions.dup }.each { |subscription| subscription.push(event) }
    end

    def unsubscribe(subscription)
      @mutex.synchronize { @subscriptions.delete(subscription) }
    end
  end
end
