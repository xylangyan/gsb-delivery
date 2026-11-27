# frozen_string_literal: true

require 'fileutils'
require_relative 'tiny_pdf'

module VolumeCombine
  # Builds the two tiny inspection source volumes.
  #
  # Volume A stores its named destinations the legacy way: a catalog level
  # /Dests dictionary (the old "table of contents dictionary" writing).
  #
  # Volume B stores them the modern way: a /Names /Dests name tree split across
  # nested /Kids leaf nodes.
  #
  # Both volumes carry byte-identical link rectangles and the same destination
  # spellings, so the auditor can prove equal-looking links are still
  # interpreted relative to their own source volume.
  class Fixtures
    def self.build(dir)
      FileUtils.mkdir_p(dir)
      builder = new
      paths = {
        'volume_a.pdf' => builder.volume_a,
        'volume_b.pdf' => builder.volume_b
      }
      paths.map { |name, data| File.join(dir, name).tap { |p| IO.binwrite(p, data) } }
    end

    def initialize
      @a = TinyPDF.new
      @b = TinyPDF.new
    end

    def volume_a
      pages_root = @a.alloc
      refs = allocate_pages(@a)
      build_note_page(@a, 'A', '期A', refs, pages_root)
      build_tail_page(@a, 'A', '期A', refs, pages_root)
      build_sample_page(@a, 'A', '期A', refs, pages_root)
      build_data_page(@a, 'A', '期A', refs, pages_root)
      catalog = {
        Pages: pages_root,
        # Legacy catalog dictionary: name -> explicit destination array.
        Dests: {
          :"读数" => [refs[:p3], :FitH, 700],
          :"锚点甲" => [refs[:p4], :Fit]
        },
        Outlines: outlines(@a, 'A', refs)
      }
      @a.render(catalog, page_refs: refs.values_at(:p1, :p2, :p3, :p4), nested: true)
    end

    def volume_b
      pages_root = @b.alloc
      refs = allocate_pages(@b)
      build_note_page(@b, 'B', '期B', refs, pages_root)
      build_tail_page(@b, 'B', '期B', refs, pages_root)
      build_sample_page(@b, 'B', '期B', refs, pages_root)
      build_data_page(@b, 'B', '期B', refs, pages_root)

      # Modern name tree, deliberately split across two /Kids leaf nodes.
      leaf_one = @b.add({ Names: [+"读数", [refs[:p3], :XYZ, 80, 700, 1.5]] })
      # B re-uses "锚点甲" on purpose: an A link losing its A4 target must
      # never be silently redirected to this other-period same name.
      leaf_two = @b.add({ Names: [+"锚点甲", [refs[:p4], :Fit], +"锚点乙", [refs[:p4], :FitV, 90]] })
      dests_root = @b.add({ Kids: [leaf_one, leaf_two] })
      names = @b.add({ Type: :Names, Dests: dests_root })

      catalog = { Pages: pages_root, Names: names, Outlines: outlines(@b, 'B', refs) }
      @b.render(catalog, page_refs: refs.values_at(:p1, :p2, :p3, :p4), nested: true)
    end

    private

    def allocate_pages(pdf)
      %i[p1 p2 p3 p4].each_with_object({}) { |key, memo| memo[key] = pdf.alloc }
    end

    # Instruction page (placed twice in the combined volume). Its three links
    # cover the three legal target kinds: page object, name, integer page.
    def build_note_page(pdf, tag, period, refs, pages_root)
      annots = [
        pdf.link_annotation([70, 700, 300, 720], { dest: [refs[:p3], :Fit], direct: true }),
        pdf.link_annotation([70, 670, 300, 690], { dest: +"读数", direct: false }),
        pdf.link_annotation([70, 640, 300, 660], { dest: [2, :FitH, 500], direct: true })
      ]
      pdf.page("#{tag}-NOTE", "#{period} 说明页", pages_root, ref: refs[:p1], annots: annots)
    end

    def build_tail_page(pdf, tag, period, refs, pages_root)
      annots = [
        pdf.link_annotation([70, 700, 300, 720], { uri: "https://example.com/wo/#{tag}-P2" }),
        pdf.link_annotation([70, 670, 300, 690], { dest: [0, :Fit], direct: true }),
        pdf.link_annotation([70, 640, 300, 660], { dest: +"锚点甲", direct: false })
      ]
      pdf.page("#{tag}-TAIL2", "#{period} 普通页", pages_root, ref: refs[:p2], annots: annots)
    end

    def build_sample_page(pdf, tag, period, refs, pages_root)
      annots = [
        pdf.link_annotation([70, 700, 300, 720], { dest: [refs[:p1], :Fit], direct: true }),
        pdf.link_annotation([70, 670, 300, 690], { uri: "https://example.com/wo/#{tag}-P3" })
      ]
      pdf.page("#{tag}-SAMPLE", "#{period} 抽样页", pages_root, ref: refs[:p3], annots: annots)
    end

    def build_data_page(pdf, tag, period, refs, pages_root)
      annots = [
        pdf.link_annotation([70, 700, 300, 690], { dest: [refs[:p3], :XYZ, 0, 792, 2], direct: false })
      ]
      pdf.page("#{tag}-DATA", "#{period} 数据页", pages_root, ref: refs[:p4], annots: annots)
    end

    def outlines(pdf, tag, refs)
      root = pdf.alloc
      section = pdf.alloc
      node_note = pdf.alloc
      node_sample = pdf.alloc
      node_data = pdf.alloc

      pdf.set(node_data.id, { Title: +"#{tag} 数据书签", Parent: section, Dest: [refs[:p4], :Fit] })
      pdf.set(section.id, {
        Title: +"#{tag} 附录", Parent: root,
        Prev: node_sample, First: node_data, Last: node_data, Count: 1
      })
      pdf.set(node_note.id, {
        Title: +"#{tag} 说明书签", Parent: root, Next: node_sample, Dest: [refs[:p1], :Fit]
      })
      sample_dest = tag == 'A' ? [refs[:p3], :FitH, 700] : [refs[:p3], :XYZ, 80, 700, 1.5]
      pdf.set(node_sample.id, {
        Title: +"#{tag} 抽样书签", Parent: root, Prev: node_note, Next: section, Dest: sample_dest
      })
      pdf.set(root.id, { Type: :Outlines, First: node_note, Last: section, Count: 3 })
      root
    end
  end
end
