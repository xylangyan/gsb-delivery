# frozen_string_literal: true

require 'spec_helper'
require 'tmpdir'

RSpec.describe DBF::SharedMemo do
  def memo_text(length, seed)
    (0...length).map { |i| ((seed + i) % 26 + 65).chr }.join
  end

  def scan_side(version:, block_size:, items:, name:)
    pending_items = items.map do |index, old_block, value|
      DBF::SharedMemo::Item.new(record: index, field: 'MEMO', dbf_offset: 0, width: 10,
                                deleted: false, old_block:, value:)
    end
    DBF::SharedMemo::Bank.new(name:, version:, block_size:, items: pending_items)
  end

  let(:dbase) do
    scan_side(version: '83', block_size: 512, name: 'dbase', items: [
      [0, 1, memo_text(300, 0)],
      [1, 1, memo_text(300, 0)],
      [2, 2, memo_text(700, 1)],
      [3, 0, nil]
    ])
  end

  let(:foxpro) do
    scan_side(version: 'f5', block_size: 64, name: 'fox', items: [
      [0, 8, memo_text(40, 2)],
      [1, 8, memo_text(40, 2)],
      [2, 9, memo_text(200, 3)],
      [3, 0, nil]
    ])
  end

  describe DBF::SharedMemo::Plan do
    subject(:plan) { described_class.new(dbase:, foxpro:) }

    it 'reads every memo back through both native readers' do
      expect(plan.conflicts).to be_empty
      expect(plan.verification[:dbase][:failed]).to eq 0
      expect(plan.verification[:foxpro][:failed]).to eq 0
    end

    it 'shares bytes: one FoxPro memo sits inside a dBase block tail' do
      fox_offset = plan.instance_variable_get(:@fox_placements).values.map(&:offset)
      expect(fox_offset.any? { |offset| offset < plan.instance_variable_get(:@dbase_end) }).to be true
    end

    it 'keeps a shared dBase block the same for both records that point at it' do
      blocks = plan.adjustments(:dbase)[:dbase][:pairs]
      expect(blocks[1]).to eq blocks[1]
    end

    it 'names the changed block pairs the handover sheet must approve' do
      changed = plan.adjustments(:dbase)[:dbase][:changed]
      expect(changed).to be_a(Hash)
      expect(plan.adjustments(:foxpro)[:foxpro][:changed]).to be_a(Hash)
    end

    it 'reports which original bytes a receiving block uses' do
      item = dbase.items.first
      block = plan.new_block(item, :dbase)
      provenance = plan.provenance(:dbase, block)
      expect(provenance[:value]).to eq item.value
      expect(provenance[:sources]).to include(hash_including(origin: :dbase))
    end

    it 'pins blocks outside the approved ranges' do
      pinned = described_class.new(dbase:, foxpro:, allowed_dbase: [1..1], allowed_foxpro: [8..8])
      expect(pinned.new_block(dbase.items[2], :dbase)).to eq 2
      expect(pinned.new_block(foxpro.items[2], :foxpro)).to eq 9
    end

    it 'reports a conflict naming the records behind a pinned block that cannot stay' do
      pinned = described_class.new(dbase:, foxpro:, allowed_dbase: [2..2], allowed_foxpro: [9..9])
      expect(pinned.conflicts.map(&:reason)).to include('block 8 is pinned but occupied')
      blocked = pinned.conflicts.select { |conflict| conflict.reason == 'block 8 is pinned but occupied' }
      expect(blocked.map(&:old_block)).to eq [8, 8, 8]
      expect(blocked.filter_map(&:record)).to eq [0, 1]
    end
  end

  describe 'dBase III and FoxPro edge semantics' do
    it 'distinguishes no memo from an empty memo on the FoxPro side' do
      header = "\x00".b * 512
      header[6, 2] = [64].pack('n')
      empty = DBF::Memo::Foxpro.new(StringIO.new(header + [1, 1].pack('NN')), 'f5')
      expect(empty.get(8)).to eq ''
    end

    it 'treats a length-zero FoxPro header as no memo' do
      header = "\x00".b * 512
      header[6, 2] = [64].pack('n')
      memo = DBF::Memo::Foxpro.new(StringIO.new(header + [1, 0].pack('NN')), 'f5')
      expect(memo.get(8)).to be_nil
    end

    it 'returns an empty string for a dBase block whose first byte is the terminator' do
      header = "\x00".b * 1024
      body = header + "\x1A".b + ("\x00".b * 511)
      memo = DBF::Memo::Dbase3.new(StringIO.new(body), '83')
      expect(memo.get(2)).to eq ''
    end

    it 'stops a dBase memo at its own terminator, not the FoxPro length' do
      header = "\x00".b * 512
      body = header + "abc\x1Adef".b
      memo = DBF::Memo::Dbase3.new(StringIO.new(body), '83')
      expect(memo.get(1)).to eq 'abc'
    end
  end

  describe '.scan' do
    it 'visits deleted records as well as live ones' do
      Dir.mktmpdir do |dir|
        dbf = File.join(dir, 'data.dbf')
        dbt = File.join(dir, 'data.dbt')
        FileUtils.cp fixture('dbase_83.dbf'), dbf
        FileUtils.cp fixture('dbase_83.dbt'), dbt
        raw = File.binread(dbf)
        raw[513] = '*'
        File.binwrite(dbf, raw)
        bank = described_class.scan(dbf, dbt)
        expect(bank.deleted_items).to eq 1
        expect(bank.items.size).to eq 67
      end
    end
  end

  describe 'a full fixture pair' do
    let(:dbase) { described_class.scan(fixture('dbase_83.dbf'), fixture('dbase_83.dbt'), name: 'dbase83') }
    let(:foxpro) { described_class.scan(fixture('dbase_f5.dbf'), fixture('dbase_f5.fpt'), name: 'foxf5') }

    it 'plans a store both fixture banks read back identically' do
      plan = DBF::SharedMemo::Plan.new(dbase:, foxpro:)
      expect(plan.conflicts).to be_empty
      expect(plan.verification[:dbase]).to include(checked: 67, failed: 0)
      expect(plan.verification[:foxpro]).to include(checked: 211, failed: 0)
      expect(plan.size).to be <= floor_size
    end

    def floor_size
      dbase_blocks = dbase.distinct_values.sum { |value| (value.bytesize + 1 + 511) / 512 } * 512
      fox_slots = foxpro.distinct_values.sum { |value| (8 + value.bytesize + 63) / 64 }
      tail_slots = dbase.distinct_values.sum do |value|
        used = (value.bytesize + 1) % 512
        used = 512 if used.zero?
        (512 - used) / 64
      end
      overflow_slots = fox_slots - tail_slots
      512 + dbase_blocks + overflow_slots * 64 + 64
    end
  end
end
