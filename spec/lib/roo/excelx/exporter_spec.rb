# frozen_string_literal: true

require 'spec_helper'
require 'tmpdir'

describe Roo::Excelx::Exporter do
  describe 'relationship-driven part selection' do
    let(:path) { 'test/files/comments.xlsx' }

    it 'requires explicit authorized sheet names' do
      expect { described_class.new(path, sheet_names: []) }
        .to raise_error(ArgumentError, /sheet_names is required/)
    end

    it 'selects only the requested sheet' do
      plan = described_class.plan(path, sheet_names: ['Sheet1'])

      expect(plan[:selected_sheets]).to eq ['Sheet1']
      expect(plan[:excluded_sheets]).to contain_exactly('Sheet2', 'Sheet3')
    end

    it 'follows the selected sheet own comment relationship' do
      plan = described_class.plan(path, sheet_names: ['Sheet1'])
      parts = plan[:sheets].first[:parts].map { |part| part[:part] }

      expect(parts).to include('xl/worksheets/sheet1.xml', 'xl/comments1.xml')
    end

    it 'records the source, digest, and provenance chain for each part' do
      plan = described_class.plan(path, sheet_names: ['Sheet1'])
      comment = plan[:sheets].first[:parts].find { |part| part[:part] == 'xl/comments1.xml' }

      expect(comment[:source]).to eq path
      expect(comment[:sha256]).to match(/\A[0-9a-f]{64}\z/)
      expect(comment[:provenance_chain].map { |link| link[:id] }).to eq %w[rId1 rId2]
      expect(comment[:provenance_chain].last[:target]).to eq '../comments1.xml'
    end
  end

  describe 'a workbook where file numbers and owners disagree' do
    let(:source) { 'test/files/relationship-ownership.xlsx' }

    it 'takes A comments from comments2.xml, not from same-numbered comments1.xml' do
      plan = described_class.plan(source, sheet_names: ['A'])
      parts = plan[:sheets].first[:parts].map { |part| part[:part] }

      expect(parts).to include('xl/comments2.xml')
      expect(parts).not_to include('xl/comments1.xml')
    end

    it 'takes A drawing from drawing3.xml and only the image that drawing references' do
      plan = described_class.plan(source, sheet_names: ['A'])
      parts = plan[:sheets].first[:parts].map { |part| part[:part] }

      expect(parts).to include('xl/drawings/drawing3.xml', 'xl/media/image7.png')
      expect(parts).not_to include('xl/drawings/drawing1.xml', 'xl/media/image1.png')
    end

    it 'does not use unselected sheet parts to fill any gap' do
      plan = described_class.plan(source, sheet_names: ['A'])
      unauthorized = plan[:unauthorized_parts].map { |entry| entry[:part] }
      selected_parts = plan[:sheets].first[:parts].map { |part| part[:part] }

      expect(unauthorized).to include('xl/worksheets/sheet2.xml', 'xl/comments1.xml')
      expect(selected_parts).not_to include('xl/worksheets/sheet2.xml', 'xl/comments1.xml')
    end

    it 'lists no missing required parts' do
      plan = described_class.plan(source, sheet_names: ['A'])

      expect(plan[:missing_parts]).to be_empty
      expect(plan[:consistent_copy_possible]).to be true
    end

    it 'keeps the external hyperlink target external and does not copy it' do
      plan = described_class.plan(source, sheet_names: ['A'])
      hyperlink = plan[:sheets].first[:parts].find { |part| part[:reason].start_with?('hyperlink') }

      expect(hyperlink[:external]).to be true
      expect(hyperlink[:source]).to eq 'https://example.com/a'
      expect(hyperlink[:sha256]).to be_nil
    end

    it 'retains the external workbook link part and its external target' do
      plan = described_class.plan(source, sheet_names: ['A'])

      expect(plan[:workbook_parts].map { |part| part[:part] }).to include('xl/externalLinks/externalLink1.xml')
      expect(plan[:external_references].first[:external_target_copied]).to be false
    end

    it 'writes a copy that rereads to A values, A comments, A hyperlink, and A image only' do
      Dir.mktmpdir do |dir|
        destination = File.join(dir, 'a-only.xlsx')
        described_class.write(source, destination, sheet_names: ['A'])

        copy = Roo::Excelx.new(destination)
        begin
          expect(copy.sheets).to eq ['A']
          expect(copy.cell(1, 1)).to eq 'A-only'
          expect(copy.comments).to eq [[1, 1, 'A comment']]
          expect(copy.hyperlink(1, 1)).to eq 'https://example.com/a'
          expect(copy.images.compact.map { |image| File.basename(image) }).to eq ['roo_media_image7.png']
        ensure
          copy.close
        end
      end
    end

    it 'passes the cell-by-cell reread audit' do
      Dir.mktmpdir do |dir|
        destination = File.join(dir, 'a-only.xlsx')
        result = described_class.write(source, destination, sheet_names: ['A'])

        expect(result[:audit][:ok]).to be true
        expect(result[:audit][:cell_differences]).to be_empty
        expect(result[:audit][:comment_differences]).to be_empty
        expect(result[:audit][:hyperlink_differences]).to be_empty
        expect(result[:audit][:attachment_differences]).to be_empty
      end
    end

    it 'refuses to write into an unknown sheet name' do
      expect { described_class.plan(source, sheet_names: ['Z']) }
        .to raise_error(ArgumentError, /unknown sheet/)
    end

    it 'works from a loaded workbook instance' do
      workbook = Roo::Excelx.new(source)
      begin
        plan = workbook.export_plan(sheet_names: ['A'])

        expect(plan[:selected_sheets]).to eq ['A']
        expect(plan[:sheets].first[:parts].map { |part| part[:part] }).to include('xl/comments2.xml')
      ensure
        workbook.close
      end
    end
  end

  describe 'shared part feasibility' do
    let(:source) { 'test/files/shared-parts.xlsx' }

    it 'reports shared strings as strictly splittable with remapping' do
      plan = described_class.plan(source, sheet_names: ['A'])
      shared_strings = plan[:shared_parts].find { |part| part[:kind] == 'shared_strings' }

      expect(shared_strings[:strict_subset_copy_self_consistent]).to be true
      expect(shared_strings[:strict_subset_requires_rewrite]).to be true
      expect(shared_strings[:strategy]).to eq 'prune_to_referenced_entries_and_remap_cell_indices'
    end

    it 'cannot derive a strict subset from the shared comments part' do
      plan = described_class.plan(source, sheet_names: ['A'])
      comments = plan[:shared_parts].find { |part| part[:kind] == 'comments' }

      expect(comments[:shared_with_sheets]).to contain_exactly('A', 'B')
      expect(comments[:strict_subset_copy_self_consistent]).to be false
      expect(comments[:whole_part_structural_copy_valid]).to be true
      expect(comments[:whole_part_carries_unselected_content]).to be true
      expect(comments[:verdict]).to eq 'strict_subset_not_derivable'
    end

    it 'cannot slice one anchor set out of a drawing shared by A and B' do
      plan = described_class.plan(source, sheet_names: ['A'])
      drawing = plan[:shared_parts].find { |part| part[:kind] == 'drawing' }

      expect(drawing[:verdict]).to eq 'whole_part_required_no_partial_slice'
      expect(drawing[:strict_subset_copy_self_consistent]).to be false
    end

    it 'states that a strict A-only copy is not possible for this workbook' do
      plan = described_class.plan(source, sheet_names: ['A'])

      expect(plan[:scope][:strict][:status]).to eq 'not_possible'
      expect(plan[:scope][:strict][:blocking_parts].map { |part| part[:part] })
        .to include('xl/comments1.xml', 'xl/drawings/drawing1.xml')
      expect(plan[:scope][:complete][:status]).to eq 'possible'
    end

    it 'refuses a strict write and names the blocking parts' do
      Dir.mktmpdir do |dir|
        expect {
          described_class.write(source, File.join(dir, 'strict.xlsx'), sheet_names: ['A'], scope: :strict)
        }.to raise_error(Roo::ExportError, /xl\/comments1\.xml/)
      end
    end

    it 'still writes a complete copy and records that shared content was retained' do
      Dir.mktmpdir do |dir|
        result = described_class.write(source, File.join(dir, 'complete.xlsx'), sheet_names: ['A'])

        expect(result[:audit][:ok]).to be true
        expect(result[:plan][:shared_parts].map { |part| part[:part] }).to include('xl/comments1.xml')
      end
    end
  end
end
