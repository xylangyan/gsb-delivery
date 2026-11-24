# frozen_string_literal: true

require_relative 'lib/fugit'

Solution = Fugit::PhasePlanner unless Object.const_defined?(:Solution)

if $PROGRAM_NAME == __FILE__
  input_path = ARGV[0]

  input =
    if input_path
      JSON.parse(File.read(input_path))
    else
      JSON.parse($stdin.read)
    end

  result = Solution.new(input).plan
  puts JSON.pretty_generate(result.to_h)
  exit(result.success? ? 0 : 2)
end
