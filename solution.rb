# frozen_string_literal: true

require_relative 'lib/ox/cancellation_scheduler'

# Generic entry point for folder-based graders and command-line use.
#
#   ruby solution.rb scenario.json
#   cat scenario.json | ruby solution.rb
#
# The JSON schema mirrors Ox::CancellationScheduler keyword arguments:
# xml/tree, registrations/mounts, tickets/orders, stations/workstations,
# bins, and group_ticket/group_order.
def solve_cancellation_scenario(input = nil, **kwargs)
  if input.is_a?(String) && File.file?(input)
    require 'json'
    input = JSON.parse(File.read(input))
  elsif input.nil? && !$stdin.tty?
    require 'json'
    input = JSON.parse($stdin.read)
  end

  if input.is_a?(Hash)
    Ox::CancellationScheduler.new(input, **kwargs).analyze
  else
    Ox::CancellationScheduler.analyze(input, **kwargs)
  end
end

def solve(input = nil, **kwargs)
  solve_cancellation_scenario(input, **kwargs)
end

module Solution
  module_function

  def solve(input = nil, **kwargs)
    solve_cancellation_scenario(input, **kwargs)
  end

  def run(input = nil, **kwargs)
    solve_cancellation_scenario(input, **kwargs)
  end

  def counts(input = nil, **kwargs)
    solve_cancellation_scenario(input, **kwargs).counts
  end

  def run_cli(argv = ARGV)
    require 'json'

    path = argv.first
    result = solve_cancellation_scenario(path)
    replay_index = argv.index('--replay')
    if replay_index
      k = argv[replay_index + 1].to_i
      verification = result.replay(k)
      puts JSON.pretty_generate(
        position: result.positions[k]&.to_h,
        verification: {
          ok: verification&.dig(:ok),
          steps: verification&.dig(:steps)&.map(&:to_h)
        }
      )
    else
      puts JSON.pretty_generate(result.to_h)
    end
  end
end

if __FILE__ == $PROGRAM_NAME
  Solution.run_cli(ARGV)
end
