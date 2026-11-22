# frozen_string_literal: true

require 'spec_helper'

describe 'Roo::Excelx shared formulas' do
  let(:v1) { Roo::Excelx.new('test/files/shared_formula_cache.xlsx') }
  let(:v2) { Roo::Excelx.new('test/files/shared_formula_cache_v2.xlsx') }

  describe 'formula identity' do
    it 'marks the master as a shared formula anchored at its own coordinate' do
      info = v1.formula_info('C', 4)

      expect(info.kind).to eq(:shared)
      expect(info.master).to eq([4, 3])
      expect(info.shared_formula_index).to eq('0')
      expect(info.shared_formula_ref).to eq('C4:C9')
      expect(info.literal).to eq('SUM($A4:B4)+"x"')
      expect(v1.shared_formula?('C', 4)).to be true
    end

    it 'marks an explicit formula inside the shared range as explicit' do
      info = v1.formula_info('C', 5)

      expect(info.kind).to eq(:explicit)
      expect(info.master).to eq([5, 3])
      expect(info.literal).to eq('SUM($A5:B5)*2')
      expect(info.shared_formula_index).to be_nil
      expect(v1.shared_formula?('C', 5)).to be false
    end

    it 'keeps the master literal available on dependents' do
      info = v1.formula_info('C', 8)

      expect(info.kind).to eq(:shared)
      expect(info.master).to eq([4, 3])
      expect(info.literal).to eq('SUM($A4:B4)+"x"')
      expect(info.formula).to eq('SUM($A8:B8)+"x"')
    end
  end

  describe 'relative reference translation' do
    it 'shifts relative row and column references by the actual offset' do
      expect(v1.formula('C', 4)).to eq('SUM($A4:B4)+"x"')
      expect(v1.formula('C', 6)).to eq('SUM($A6:B6)+"x"')
      expect(v1.formula('C', 8)).to eq('SUM($A8:B8)+"x"')
    end

    it 'leaves absolute references and string literals unchanged' do
      expect(v1.formula('C', 8)).to include('$A8')
      expect(v1.formula('C', 8)).to include('"x"')
    end

    it 'translates a formula shifted by both row and column' do
      master = Roo::Excelx::Coordinate.new(4, 5)
      target = Roo::Excelx::Coordinate.new(6, 7)

      translated = Roo::Excelx::FormulaTranslator.translate('C4-B4-D4', master, target)

      expect(translated).to eq('E6-D6-F6')
    end

    it 'does not mistake function names, named ranges or table columns for cells' do
      master = Roo::Excelx::Coordinate.new(1, 1)
      target = Roo::Excelx::Coordinate.new(2, 1)

      expect(Roo::Excelx::FormulaTranslator.translate('LOG10(A1)+Rate*Table1[Col]', master, target))
        .to eq('LOG10(A2)+Rate*Table1[Col]')
    end

    it 'translates sheet-qualified references but preserves the sheet name' do
      master = Roo::Excelx::Coordinate.new(1, 1)
      target = Roo::Excelx::Coordinate.new(3, 2)

      expect(Roo::Excelx::FormulaTranslator.translate("'My Sheet'!A1+Sheet2!$B$2", master, target))
        .to eq("'My Sheet'!B3+Sheet2!$B$2")
    end

    it 'reports #REF! when a translated reference would leave the grid' do
      master = Roo::Excelx::Coordinate.new(1, 1)
      target = Roo::Excelx::Coordinate.new(1, 1)

      expect(Roo::Excelx::FormulaTranslator.translate('A1', master, target)).to eq('A1')

      translator = Roo::Excelx::FormulaTranslator
      expect(translator.translate('A1', Roo::Excelx::Coordinate.new(5, 2), Roo::Excelx::Coordinate.new(5, 1)))
        .to eq('#REF!')
    end
  end

  describe 'cached values' do
    it 'keeps a present cache and marks it as cached, not recalculated' do
      info = v1.formula_info('C', 8)

      expect(info.cache).to eq(:present)
      expect(info.calculated).to be false
      expect(info.value_source).to eq(:cached)
      expect(info.freshness).to eq(:unverified)
      expect(v1.cell('C', 8)).to eq(60)
      expect(v1.formula_cached?('C', 8)).to be true
    end

    it 'distinguishes an empty cache from a missing one' do
      empty = v1.formula_info('C', 6)
      missing = v1.formula_info('C', 7)

      expect(empty.cache).to eq(:empty)
      expect(missing.cache).to eq(:missing)
      expect(empty.value_source).to eq(:none)
      expect(missing.value_source).to eq(:none)
    end

    it 'does not turn an empty or missing cache into zero or a date' do
      expect(v1.cell('C', 6)).to eq('')
      expect(v1.cell('C', 7)).to be_nil
      expect(v1.celltype('C', 6)).to eq(:formula)
      expect(v1.celltype('C', 7)).to eq(:formula)
      expect(v1.formula_cached?('C', 6)).to be true
      expect(v1.formula_cached?('C', 7)).to be false
    end
  end

  describe 'narrowed shared range' do
    it 'resolves cells inside the narrowed ref' do
      expect(v2.formula('C', 4)).to eq('SUM($A4:B4)+"x"')
      expect(v2.formula('C', 6)).to eq('SUM($A6:B6)+"x"')
      expect(v2.formula_info('C', 6).shared_formula_ref).to eq('C4:C6')
    end

    it 'does not rebuild stale dependents from the old anchor' do
      [7, 8, 9].each do |row|
        info = v2.formula_info('C', row)

        expect(info.kind).to eq(:shared)
        expect(info.resolved).to be false
        expect(info.unresolved_reason).to eq(:shared_formula_out_of_range)
        expect(info.master).to be_nil
        expect(info.literal).to be_nil
        expect(v2.formula('C', row)).to be_nil
        expect(v2.formula?('C', row)).to be true
      end
    end

    it 'keeps the cached value of a stale dependent when one was stored' do
      expect(v2.formula_info('C', 8).cache).to eq(:present)
      expect(v2.cell('C', 8)).to eq(60)
      expect(v2.formula('C', 8)).to be_nil
    end

    it 'agrees with the same cells read one by one after streaming' do
      streamed = {}
      v2.each_row_streaming(offset: 4, max_rows: 5, sheet: 'Monthly') do |row|
        cell = row.find { |candidate| candidate.coordinate.column == 3 }
        streamed[cell.coordinate.row] = [cell.formula, cell.value, cell.formula_info.to_h]
      end

      (5..9).each do |row|
        cell = v2.sheet_for('Monthly').cells[[row, 3]]
        expect(streamed.fetch(row)).to eq([cell.formula, cell.value, cell.formula_info.to_h])
      end
    end
  end

  describe 'streaming a window whose master is outside it' do
    it 'resolves dependents from the master in a separate pass' do
      formulas = {}
      v1.each_row_streaming(offset: 4, max_rows: 5, sheet: 'Monthly') do |row|
        cell = row.find { |candidate| candidate.coordinate.column == 3 }
        formulas[cell.coordinate.row] = cell.formula
      end

      expect(formulas.keys).to eq([5, 6, 7, 8, 9])
      expect(formulas[6]).to eq('SUM($A6:B6)+"x"')
      expect(formulas[8]).to eq('SUM($A8:B8)+"x"')
    end

    it 'reports the same cache state as non-streaming reads' do
      streamed = {}
      v1.each_row_streaming(offset: 4, max_rows: 5, sheet: 'Monthly') do |row|
        cell = row.find { |candidate| candidate.coordinate.column == 3 }
        streamed[cell.coordinate.row] = cell.formula_cache_status
      end

      (5..9).each do |row|
        expect(streamed.fetch(row)).to eq(v1.formula_info('C', row).cache)
      end
    end
  end

  describe '#formulas' do
    it 'lists the translated formula for every resolvable formula cell' do
      expect(v1.formulas('Monthly').select { |row, column, _| column == 3 }).to eq(
        [
          [4, 3, 'SUM($A4:B4)+"x"'],
          [5, 3, 'SUM($A5:B5)*2'],
          [6, 3, 'SUM($A6:B6)+"x"'],
          [7, 3, 'SUM($A7:B7)+"x"'],
          [8, 3, 'SUM($A8:B8)+"x"'],
          [9, 3, 'SUM($A9:B9)+"x"']
        ]
      )
    end

    it 'omits a stale dependent whose literal cannot be proven' do
      expect(v2.formulas('Monthly').select { |row, column, _| column == 3 }).to eq(
        [
          [4, 3, 'SUM($A4:B4)+"x"'],
          [5, 3, 'SUM($A5:B5)*2'],
          [6, 3, 'SUM($A6:B6)+"x"']
        ]
      )
    end
  end

  describe 'the existing formula fixture' do
    let(:legacy) { Roo::Excelx.new('test/files/formula.xlsx') }

    it 'keeps explicit formulas untouched and nil when absent' do
      expect(legacy.formula('A', 7)).to eq('SUM(A1:A6)')
      expect(legacy.formula('B', 7)).to eq('SUM($A$1:B6)')
      expect(legacy.formula('A', 6)).to be_nil
      expect(legacy.formula_info('A', 7).kind).to eq(:explicit)
    end
  end

  describe 'an existing workbook with a shared range' do
    let(:simple) { Roo::Excelx.new('test/files/simple_spreadsheet.xlsx') }

    it 'translates every dependent instead of returning an empty string' do
      expect(simple.formula('E', 4)).to eq('C4-B4-D4')
      expect(simple.formula('E', 5)).to eq('C5-B5-D5')
      expect(simple.formula('E', 13)).to eq('C13-B13-D13')
      expect(simple.formula_info('E', 13).cache).to eq(:present)
    end
  end
end
