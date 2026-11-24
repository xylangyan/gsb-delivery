# frozen_string_literal: true

require 'helper'
require 'json'

$LOAD_PATH.unshift(File.expand_path('../../tools', __dir__))
require 'bibmerge'

module BibMerge
  # Differential test: for small random inputs, enumerate every macro
  # selection and parent choice, then check the solver's cost matches the
  # true minimum and its output reproduces every institution view.
  class OptimalityTest < Minitest::Spec
    def build(dir, macro_values, parents, children)
      %w[a b].each do |id|
        lines = []
        macro_values.each_with_index do |values, mi|
          lines << "@string{ m#{mi}:base = #{values[id][0].inspect} }"
          lines << "@string{ m#{mi}:proc = #{(values[id][1] + ' # ').inspect} # m#{mi}:base }"
        end
        children[id].each do |key, parent|
          lines << "@inproceedings{#{key}, crossref = {#{parent}}, author = \"X\", title = \"T\"}"
        end
        parents.each_with_index do |pair, pi|
          lines << "@proceedings{#{pair[id]}, title = m#{pi}:proc, year = 2024}"
        end
        FileUtils.mkdir_p(File.join(dir, id))
        File.write(File.join(dir, id, 'lib.bib'), lines.join("\n"))
      end
    end

    def manifest_for(macro_count, parent_count, children)
      {
        'institutions' => [
          { 'id' => 'a', 'file' => 'a/lib.bib' },
          { 'id' => 'b', 'file' => 'b/lib.bib' }
        ],
        'macros' => (0...macro_count).flat_map do |mi|
          [{ 'formal' => "m#{mi}:base",
             'candidates' => [{ 'institution' => 'a', 'key' => "m#{mi}:base" },
                              { 'institution' => 'b', 'key' => "m#{mi}:base" }] },
           { 'formal' => "m#{mi}:proc",
             'candidates' => [{ 'institution' => 'a', 'key' => "m#{mi}:proc" },
                              { 'institution' => 'b', 'key' => "m#{mi}:proc" }] }]
        end,
        'conferences' => [{ 'formal' => 'c2024',
                            'parents' => (0...parent_count).map do |pi|
                              { 'institution' => pi.even? ? 'a' : 'b', 'key' => "p#{pi}" }
                            end,
                            'children' => children['a'].map do |key, _|
                              { 'institution' => 'a', 'key' => key, 'formal' => "a-#{key}" }
                            end + children['b'].map do |key, _|
                              { 'institution' => 'b', 'key' => key, 'formal' => "b-#{key}" }
                            end }],
        'citations' => []
      }
    end

    def true_minimum(merger)
      conference = merger.instance_variable_get(:@conferences)['c2024']
      macros = merger.instance_variable_get(:@macros).keys
      costs = []
      merger.send(:macro_assignments, macros).each do |assignment|
        expanded = merger.send(:expansions, assignment)
        next if expanded.nil?

        ([nil] + (0...conference.parents.length).to_a).each do |pi|
          plan = merger.send(:plan_conference, conference, pi, assignment, expanded)
          costs << plan.cost if plan
        end
      end
      costs.min
    end

    it 'matches the brute-force minimum on random small inputs' do
      rng = Random.new(42)
      8.times do
        Dir.mktmpdir do |dir|
          macro_count = rng.rand(1..2)
          parent_count = rng.rand(1..3)
          values = Array.new(macro_count) do
            {
              'a' => ["A#{rng.rand(9)}", "Proc A#{rng.rand(9)} "],
              'b' => ["B#{rng.rand(9)}", "Proc B#{rng.rand(9)} "]
            }
          end
          children = {
            'a' => { 'a1' => 'p0' },
            'b' => { 'b1' => "p#{rng.rand(parent_count)}" }
          }
          parents = Array.new(parent_count) { |pi| { 'a' => "p#{pi}", 'b' => "p#{pi}" } }

          build(dir, values, parents, children)
          handover = manifest_for(macro_count, parent_count, children)
          merger = Merger.new(handover, root: dir).run

          expected = true_minimum(merger)
          actual = merger.report.dig('counts', 'changed_child_fields')
          _(actual).must_equal expected

          path = File.join(dir, 'out.bib')
          File.write(path, merger.to_bibtex)
          _(merger.verify_file(path)['status']).must_equal 'ok'
        end
      end
    end
  end
end
