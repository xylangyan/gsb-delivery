# frozen_string_literal: true

require "test/unit"
require "tmpdir"
require "fileutils"
require "rbconfig"
require_relative "../lib/archive_handoff"

class ArchiveHandoffCommandRunnerTest < Test::Unit::TestCase
  RUBY = RbConfig.ruby

  def setup
    @dir = Dir.mktmpdir("archive-commands-")
    @store = ArchiveHandoff::Store.new(@dir)
    @progress = ArchiveHandoff::Progress.new
    @runner = ArchiveHandoff::CommandRunner.new(@store, progress: @progress)
  end

  def teardown
    @runner.stop_all
    FileUtils.remove_entry(@dir) if @dir && File.directory?(@dir)
  end

  def test_stderr_from_three_commands_can_be_replayed_in_order
    fetch = [RUBY, "-e", "STDERR.print('fetch-a'); sleep 0.01; STDERR.print('fetch-b')"]
    normalize = [RUBY, "-e", "STDERR.print('normalize-a'); sleep 0.01; STDERR.print('normalize-b')"]
    package = [RUBY, "-e", "STDERR.print('package-a'); sleep 0.01; STDERR.print('package-b')"]

    @runner.run("fetch", fetch)
    @runner.run("normalize", normalize, stdin_data: "whole-volume")
    @runner.run("package", package, stdin_data: "package-input")

    assert_equal "fetch-afetch-b", stderr_for("fetch")
    assert_equal "normalize-anormalize-b", stderr_for("normalize")
    assert_equal "package-apackage-b", stderr_for("package")
    assert_equal "fetch-afetch-bnormalize-anormalize-bpackage-apackage-b",
                 ArchiveHandoff::Runner.new(@dir).stderr_text
  end

  def test_paused_progress_subscription_does_not_block_command
    received = Queue.new
    subscription = @progress.subscribe { |event| received << event }
    subscription.pause

    result = @runner.run(
      "fetch",
      [RUBY, "-e", "100.times { STDERR.puts('progress-line') }"]
    )

    assert_predicate result.status, :success?
    assert_operator ArchiveHandoff::Runner.new(@dir).stderr_events("fetch").length, :>=, 1
    assert_equal 100, ArchiveHandoff::Runner.new(@dir).stderr_text("fetch").lines.length
    subscription.resume
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + 2
    count = 0
    while Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline && count < 100
      event = received.pop(true) rescue nil
      count += 1 if event
    end
    assert_operator count, :>=, 1
  ensure
    subscription&.close
  end

  def test_stop_terminates_same_group_helper_holding_stderr_write_end
    script = <<~RUBY
      helper = spawn(
        #{RUBY.dump},
        "-e",
        "STDOUT.reopen(File::NULL); sleep 30",
        [:out, :err] => $stderr
      )
      STDOUT.puts(helper)
      STDOUT.flush
    RUBY

    execution = @runner.start("package", [RUBY, "-e", script])
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + 3
    helper_pid = nil
    while Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
      output = execution.stdout
      if output =~ /\A(\d+)/
        helper_pid = Regexp.last_match(1).to_i
        break
      end
      sleep 0.02
    end

    refute_nil helper_pid
    wait_for { execution.done? }
    assert execution.done?

    @runner.stop_all

    refute execution.wait_thread.alive?
    wait_for(grace: 2) { process_dead?(helper_pid) }
    assert process_dead?(helper_pid)
  end

  private

  def stderr_for(argument)
    events = @store.read_log.select { |event| event["type"] == "stderr" && event["command"] == argument }
    events.sort_by! { |event| event["seq"] || 0 }
    events.map { |event| [event["chunk_hex"]].pack("H*") }.join
  end

  def wait_for(grace: 3)
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + grace
    loop do
      return if yield
      break if Process.clock_gettime(Process::CLOCK_MONOTONIC) >= deadline

      sleep 0.02
    end
  end

  def process_dead?(pid)
    Process.kill(0, pid)
    false
  rescue Errno::ESRCH
    true
  rescue Errno::EPERM
    false
  end
end
