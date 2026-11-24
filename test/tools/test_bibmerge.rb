# frozen_string_literal: true

require 'helper'
require 'json'

$LOAD_PATH.unshift(File.expand_path('../../tools', __dir__))
require 'bibmerge'

module BibMerge
  class MergeTest < Minitest::Spec
    ROOT = File.expand_path('../../merge-example', __dir__)

    def manifest
      JSON.parse(File.read(File.join(ROOT, 'manifest.json'), encoding: 'utf-8'))
    end

    def merge
      Merger.new(manifest, root: ROOT).run
    end

    def write_temp(merger)
      path = File.join(Dir.mktmpdir, 'merged.bib')
      File.write(path, merger.to_bibtex, encoding: 'utf-8')
      path
    end

    describe 'the worked example' do
      before do
        @merger = merge
        @path = write_temp(@merger)
        @bib = BibTeX.open(@path, parse_names: false, parse_months: false)
        @bib.replace
        @bib.join
      end

      it 'reopens and verifies every institution view' do
        _(@merger.verify_file(@path)['status']).must_equal 'ok'
      end

      it 'keeps only manifest macro names, each defined once' do
        _(@bib.strings.keys.sort).must_equal %i[conf:base conf:icse ws:base ws:proc]
        _(@bib.data.count { |e| e.is_a?(BibTeX::String) }).must_equal 4
      end

      it 'keeps the nested macro reference verbatim' do
        raw = BibTeX.open(@path, parse_names: false, parse_months: false)
        _(raw.strings[:"conf:icse"].value.tokens).must_equal ['Proceedings of ', :"conf:base"]
      end

      it 'keeps one parent per formal conference' do
        parents = @bib.q('@proceedings')
        _(parents.length).must_equal 2
        _(parents.map(&:key).sort).must_equal %w[conf:icse-2024 conf:ws-2022]
      end

      it 'expands two-level macros through the parent' do
        _(@bib['conf:icse-2024'].title.to_s).must_equal 'Proceedings of Intl. Conf. on Software Engineering'
      end

      it 'reproduces a field the kept parent does not supply' do
        _(@bib['lovelace2024'].publisher.to_s).must_equal 'ACM'
      end

      it 'does not leak another institution parent title into booktitle' do
        _(@bib['turing2024'].get(:booktitle).to_s).must_equal 'Proceedings of Intl. Conf. on Software Engineering'
        _(@bib['lovelace2024'].get(:booktitle).to_s).must_equal 'Proc. International Conference on Software'
      end

      it 'replaces an explicit macro expression whose kept expansion changed' do
        change = @merger.report['child_changes'].find { |c| c['field'] == 'booktitle' }
        _(change['action']).must_equal 'replace'
        _(change['from']).must_equal 'conf:icse'
        _(change['to']).must_equal 'Proc. International Conference on Software'
      end

      it 'uses the cheapest macro and parent pair' do
        _(@merger.report.dig('counts', 'changed_child_fields')).must_equal 2
        _(@merger.report.dig('counts', 'crossref_deletions')).must_equal 0
        _(@merger.report['parents'].first['institution']).must_equal 'beta'
      end

      it 'keeps unrelated macro and conference groups separate' do
        groups = @merger.report['components'].reject { |g| g['macros'].empty? }
        _(groups.length).must_equal 2
        _(groups.map { |g| g['conferences'] }).must_equal [['conf:icse-2024'], ['conf:ws-2022']]
      end
    end

    describe 'when the same formal key has two visibly different records' do
      it 'reports the field and the institution that has it' do
        Dir.mktmpdir do |dir|
          FileUtils.mkdir_p(File.join(dir, 'a'))
          FileUtils.mkdir_p(File.join(dir, 'b'))
          File.write(File.join(dir, 'a', 'x.bib'),
                     '@proceedings{p, title = "C", year = 2024}' \
                     '@inproceedings{c, crossref = {p}, author = "A", title = "T"}')
          File.write(File.join(dir, 'b', 'x.bib'),
                     '@proceedings{q, title = "C", year = 2024}' \
                     '@inproceedings{d, crossref = {q}, author = "A", title = "T", note = "B only"}')
          handover = {
            'institutions' => [
              { 'id' => 'a', 'file' => 'a/x.bib' },
              { 'id' => 'b', 'file' => 'b/x.bib' }
            ],
            'macros' => [],
            'conferences' => [
              { 'formal' => 'c2024',
                'parents' => [
                  { 'institution' => 'a', 'key' => 'p' },
                  { 'institution' => 'b', 'key' => 'q' }
                ],
                'children' => [
                  { 'institution' => 'a', 'key' => 'c', 'formal' => 'k' },
                  { 'institution' => 'b', 'key' => 'd', 'formal' => 'k' }
                ] }
            ],
            'citations' => []
          }

          error = _(proc { Merger.new(handover, root: dir).run }).must_raise(UnrepresentableError)
          _(error.conflicts.first['field']).must_equal 'note'
          _(error.conflicts.first['extra_institution']).must_equal 'b'
        end
      end
    end
  end
end
