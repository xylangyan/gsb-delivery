# frozen_string_literal: true

require 'spec_helper'

RSpec.describe Prawn::Settlement do
  def build_pdf(entries, **overrides)
    pdf = Prawn::Document.new(page_size: [200, 160], margin: [20, 20, 20, 20])
    pdf.settlement_voucher(
      entries,
      {
        body_width: 120,
        available_height: 84,
        amount_width: 33,
        line_height: 14,
        font: 'Courier',
        font_size: 10
      }.merge(overrides)
    )
    pdf
  end

  def page_texts(pdf)
    PDF::Reader.new(StringIO.new(pdf.render)).pages.map(&:text)
  end

  let(:entry_a) { { serial: 'A', amount: 9, lines: ['A DONE'] } }
  let(:entry_b) { { serial: 'B', amount: 1, lines: ['B CHECK', 'B DONE'] } }
  let(:entry_c) { { serial: 'C', amount: -1, lines: ['C DONE'] } }
  let(:entry_d) { { serial: 'D', amount: 0, lines: ['D DONE'] } }

  context 'without the reversal entry' do
    it 'posts A on page one and carries B over to page two' do
      pdf = build_pdf([entry_a, entry_b, entry_d])
      expect(pdf.page_count).to eq(2)

      page1, page2 = page_texts(pdf)

      expect(page1).to include('A DONE')
      expect(page1).to include('B CHECK')
      expect(page1).not_to include('B DONE')
      expect(page1).not_to include('D DONE')
      expect(page1).to include('IN 0')
      expect(page1).to include('OUT 9')
      expect(page1).to include('END 9')

      expect(page2).to include('B DONE')
      expect(page2).to include('D DONE')
      expect(page2).to include('IN 9')
      expect(page2).to include('OUT 1')
      expect(page2).to match(/END\s+10/)
      expect(page2).to include('CONT B')
    end
  end

  context 'with the reversal entry inserted back' do
    it 'settles A, B and C on page one and only D on page two' do
      pdf = build_pdf([entry_a, entry_b, entry_c, entry_d])
      expect(pdf.page_count).to eq(2)

      page1, page2 = page_texts(pdf)

      expect(page1).to include('A DONE')
      expect(page1).to include('B CHECK')
      expect(page1).to include('B DONE')
      expect(page1).to include('C DONE')
      expect(page1).not_to include('D DONE')
      expect(page1).to include('IN 0')
      expect(page1).to include('OUT 9')
      expect(page1).to include('END 9')

      expect(page2).to include('D DONE')
      expect(page2).to include('IN 9')
      expect(page2).to include('OUT 0')
      expect(page2).to include('END 9')
      expect(page2).not_to include('CONT')
    end
  end

  context 'with an entry that must stay on one page' do
    it 'never splits its description' do
      keep_b = entry_b.merge(keep_together: true)
      pdf = build_pdf([entry_a, keep_b, entry_d])
      expect(pdf.page_count).to eq(2)

      page1, page2 = page_texts(pdf)

      expect(page1).to include('A DONE')
      expect(page1).not_to include('B CHECK')

      expect(page2).to include('B CHECK')
      expect(page2).to include('B DONE')
      expect(page2).to include('D DONE')
    end
  end

  context 'when the batch cannot fit' do
    it 'refuses to issue and names the stuck serial' do
      stuck = {
        serial: 'E',
        amount: 5,
        lines: ['E ONE', 'E TWO', 'E THREE', 'E FOUR', 'E FIVE'],
        keep_together: true
      }

      expect { build_pdf([entry_a, stuck, entry_d]) }
        .to raise_error(Prawn::Settlement::UnfittableError) { |error|
          expect(error.serial).to eq('E')
        }
    end
  end

  context 'with invalid input' do
    it 'rejects an empty batch' do
      expect { build_pdf([]) }.to raise_error(ArgumentError)
    end

    it 'rejects non-integer amounts' do
      expect { build_pdf([{ serial: 'X', amount: 1.5, lines: ['X'] }]) }
        .to raise_error(ArgumentError)
    end

    it 'rejects entries without description lines' do
      expect { build_pdf([{ serial: 'X', amount: 1, lines: [] }]) }
        .to raise_error(ArgumentError)
    end
  end

  it 'leaves the regular Prawn entry points untouched' do
    pdf = Prawn::Document.new(page_size: [200, 160], margin: 20)
    pdf.text('plain voucher')
    expect { pdf.render }.not_to raise_error
  end
end
