# frozen_string_literal: true

require "open3"
require "timeout"

module ArchiveHandoff
  class CommandRunner
    class Execution
      attr_reader :step, :pid, :wait_thread, :stdout, :stderr_chunks

      def initialize(step, stdin, stdout, stderr, wait_thread, store, progress)
        @step = step.to_s
        @stdin = stdin
        @stdout_io = stdout
        @stderr_io = stderr
        @wait_thread = wait_thread
        @pid = wait_thread.pid
        @store = store
        @progress = progress
        @stdout = +"".b
        @stderr_chunks = []
        @mutex = Mutex.new
        @reader_threads = []
        @stopped = false

        start_readers
      end

      def write_stdin(data)
        return if data.nil? || data.empty?

        @stdin.binmode
        @stdin.write(data)
      rescue Errno::EPIPE
      ensure
        @stdin.close unless @stdin.closed?
      end

      def complete_stdin
        @stdin.close unless @stdin.closed?
      end

      def done?
        !@wait_thread.alive?
      end

      def status
        @wait_thread.value
      end

      def output
        join_readers
        @stdout
      end

      def stderr
        @mutex.synchronize { @stderr_chunks.join }
      end

      def wait(timeout: nil)
        if timeout
          Timeout.timeout(timeout) { @wait_thread.join }
        else
          @wait_thread.join
        end
        join_readers
        self
      rescue Timeout::Error
        stop
        raise
      end

      def stop(grace: 0.2)
        return self if @stopped

        @stopped = true
        signal_group("TERM")
        deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + grace
        while @wait_thread.alive? && Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
          sleep 0.01
        end
        if @wait_thread.alive? || group_alive?
          signal_group("KILL")
          wait_group_dead
        end
        begin
          @wait_thread.join(1)
        rescue StandardError
          nil
        end
        [@stdin, @stdout_io, @stderr_io].each { |io| io.close unless io.closed? }
        join_readers(1)
        self
      end

      private

      def start_readers
        @reader_threads << Thread.new do
          loop do
            chunk = @stdout_io.readpartial(4096)
            @mutex.synchronize { @stdout << chunk }
          end
        rescue EOFError, IOError
        end

        @reader_threads << Thread.new do
          loop do
            chunk = @stderr_io.readpartial(4096)
            record_stderr(chunk)
          end
        rescue EOFError, IOError
        end
      end

      def record_stderr(chunk)
        event = @store.append_log(
          "type" => "stderr",
          "command" => @step,
          "pid" => @pid,
          "chunk" => chunk.scrub,
          "chunk_hex" => chunk.b.unpack1("H*")
        )
        @mutex.synchronize { @stderr_chunks << chunk }
        @progress&.publish(event)
      end

      def join_readers(timeout = nil)
        if timeout
          @reader_threads.each { |thread| thread.join(timeout) }
        else
          @reader_threads.each(&:join)
        end
      end

      def signal_group(signal)
        Process.kill(signal, -@pid)
      rescue Errno::ESRCH, Errno::EPERM
        begin
          Process.kill(signal, @pid)
        rescue Errno::ESRCH
          nil
        end
      end

      def group_alive?
        Process.kill(0, -@pid)
        true
      rescue Errno::ESRCH
        false
      rescue Errno::EPERM
        true
      end

      def wait_group_dead(timeout: 1)
        deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + timeout
        while group_alive? && Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
          sleep 0.01
        end
      end
    end

    def initialize(store, progress: nil)
      @store = store
      @progress = progress
      @active = []
      @mutex = Mutex.new
    end

    def start(step, command, stdin_data: nil, env: {})
      args = command.is_a?(Array) ? command.map(&:to_s) : [command.to_s]
      stdin, stdout, stderr, wait_thread = Open3.popen3(env, *args, pgroup: true)
      execution = Execution.new(step, stdin, stdout, stderr, wait_thread, @store, @progress)
      @mutex.synchronize { @active << execution }
      @progress&.publish("type" => "command_start", "command" => step.to_s, "pid" => wait_thread.pid)
      execution
    end

    def run(step, command, stdin_data: nil, env: {}, timeout: nil)
      execution = start(step, command, stdin_data: stdin_data, env: env)
      execution.write_stdin(stdin_data)
      execution.complete_stdin
      execution.wait(timeout: timeout)
      result = Result.new(
        step: step,
        stdout: execution.output,
        stderr: execution.stderr,
        status: execution.status
      )
      unless result.success?
        raise CommandFailed.new("#{step} command failed with status #{result.status.exitstatus}", result)
      end

      result
    ensure
      @mutex.synchronize { @active.delete(execution) } if execution
    end

    def stop_all
      @mutex.synchronize { @active.dup }.each(&:stop)
    end

    Result = Struct.new(:step, :stdout, :stderr, :status, keyword_init: true) do
      def success?
        status.success?
      end
    end
  end
end
