# frozen_string_literal: true

require_relative 'stable_pdf'

module VolumeCombine
  # Assembles the physical output PDF from the planner's decisions.
  #
  # Pages are deep copied per output slot (so a source page placed twice has
  # independent annotation dictionaries), links are rewritten to explicit
  # destination arrays, outline chapters are rebuilt per group and the output
  # name tree is authored explicitly. No source object is shared between two
  # output pages.
  class Combiner
    SKIP_KEYS = [:Parent].freeze

    def initialize(manifest, raw_models, planner)
      @manifest = manifest
      @raw_models = raw_models
      @planner = planner
      @prototypes = {}
      @output_pages = {}
      @name_tree_rows = []
    end

    def name_tree_rows
      @name_tree_rows
    end

    def build
      load_prototypes
      assemble_pages
      retarget_all_links
      outlines = build_outlines
      names = build_name_tree

      pdf = StablePDF.new
      @output_pages.sort.each { |_slot, page| pdf.insert(-1, page) }
      pdf.instance_variable_set(:@outlines, outlines)
      pdf.instance_variable_set(:@names, names)
      pdf
    end

    private

    def load_prototypes
      @manifest.sources.each do |source_id, meta|
        @prototypes[source_id] = StablePDF.new(CombinePDF::PDFParser.new(IO.binread(meta.fetch('path'))))
      end
    end

    def assemble_pages
      @manifest.slots.each do |slot|
        prototype = @prototypes.fetch(slot.source).pages[slot.page_index]
        @output_pages[slot.slot] = deep_copy_page(prototype)
      end
    end

    # Cycle safe deep copy. Foreign page references (old destinations pointing
    # at source pages) are kept as leaves and overwritten during retargeting,
    # so copying them would only duplicate whole page trees.
    def deep_copy_page(root)
      visited = {}.compare_by_identity
      copier = lambda do |value|
        case value
        when Hash
          return value if value[:Type] == :Page && !value.equal?(root)
          return visited[value] if visited.key?(value)
          copy = {}
          visited[value] = copy
          value.each { |key, sub| copy[key] = copier.call(sub) unless SKIP_KEYS.include?(key) }
          copy
        when Array
          value.map { |item| copier.call(item) }
        when String
          value.dup.force_encoding(Encoding::ASCII_8BIT)
        else
          value
        end
      end
      copier.call(root)
    end

    def retarget_all_links
      @manifest.slots.each do |slot|
        model = @raw_models.fetch(slot.source)
        page = @output_pages.fetch(slot.slot)
        annots = annotation_array(page)
        model.links.each do |link|
          next unless link.page_index == slot.page_index
          decision = @planner.link_decisions[[slot.slot, link.order]]
          annot = annots[link.order]
          annot = annot[:referenced_object] if annot.is_a?(Hash) && annot.key?(:referenced_object)
          if decision.nil?
            # Reported gap: the planner found no same-group occurrence and no
            # permitted substitute. Neutralize so it cannot reach another
            # period's same-named page.
            neutralize(annot)
          else
            retarget(annot, link, decision)
          end
        end
      end
    end

    def neutralize(annot)
      annot.delete(:A)
      annot.delete(:Dest)
    end

    def annotation_array(page)
      annots = page[:Annots]
      annots = annots[:referenced_object] if annots.is_a?(Hash) && annots.key?(:referenced_object)
      annots || []
    end

    def retarget(annot, _link, decision)
      case decision.ref_type
      when :uri
        # External URI actions are preserved byte for byte.
        nil
      else
        destination = [page_ref(decision.output_slot), *decision.view]
        if annot.key?(:A)
          annot[:A] = { S: :GoTo, D: destination }
        else
          annot[:Dest] = destination
        end
      end
    end

    def page_ref(slot_number)
      { is_reference_only: true, referenced_object: @output_pages.fetch(slot_number) }
    end

    # ---- outlines ---------------------------------------------------------

    def build_outlines
      chapters = @manifest.slots.map { |slot| [slot.group, slot.source] }.uniq
      chapter_roots = chapters.flat_map do |group, source|
        model = @raw_models.fetch(source)
        model.outline_roots.map { |node| build_outline_node(node, source, group) }.compact
      end

      return {} if chapter_roots.empty?

      # A single wiring pass over the concatenated roots; it recurses into each
      # node's nested children exactly once.
      wire_chain(chapter_roots, nil)
      root = { Type: :Outlines, Count: chapter_roots.sum { |node| subtree_count(node) } }
      root[:First] = ref_only(chapter_roots.first)
      root[:Last] = ref_only(chapter_roots.last)
      root
    end

    def build_outline_node(node, source, group)
      children = node.children.map { |child| build_outline_node(child, source, group) }.compact
      decision = @planner.bookmark_decisions[[group, source, node.id]]

      if node.target_page && decision.nil?
        # Unroutable bookmark: reported as a gap; the node and its branch are
        # omitted so the outline never points at the wrong period.
        return nil
      end

      hash = { Title: node.title.dup }
      hash[:Dest] = [page_ref(decision.output_slot), *decision.view] if decision
      hash[:__children__] = children unless children.empty?
      hash
    end

    # Recursively set Prev/Next/First/Last/Parent/Count on a sibling chain.
    def wire_chain(nodes, parent)
      nodes.each_with_index do |node, index|
        children = node.delete(:__children__) || []
        node[:Parent] = ref_only(parent) if parent
        node[:Prev] = ref_only(nodes[index - 1]) if index.positive?
        node[:Next] = ref_only(nodes[index + 1]) if index < nodes.length - 1
        if children.empty?
          node.delete(:First)
          node.delete(:Last)
          node[:Count] = 0
        else
          wire_chain(children, node)
          node[:First] = ref_only(children.first)
          node[:Last] = ref_only(children.last)
          node[:Count] = -children.sum { |child| subtree_count(child) }
        end
      end
    end

    def subtree_count(node)
      1 + (node[:First] ? subtree_chain_count(node[:First][:referenced_object]) : 0)
    end

    def subtree_chain_count(node)
      total = 0
      while node
        total += subtree_count(node)
        node = node[:Next] ? node[:Next][:referenced_object] : nil
      end
      total
    end

    def ref_only(hash)
      { is_reference_only: true, referenced_object: hash }
    end

    # ---- name tree --------------------------------------------------------

    def build_name_tree
      names_array = []
      reserved = {}

      @planner.work_order_decisions.each do |name, decision|
        key = name.dup.force_encoding(Encoding::ASCII_8BIT)
        claim(reserved, key, decision.output_slot)
        names_array << key
        names_array << [page_ref(decision.output_slot), *normalized_view(decision.view)]
        @name_tree_rows << { kind: :work_order, name: name, slot: decision.output_slot,
                             basis: decision.basis, detail: decision.detail }
      end

      @manifest.sources.each_key do |source|
        group = chapter_group(source)
        @raw_models.fetch(source).name_dests.each do |dest|
          slot, basis, detail = @planner.route(source, dest.page_index, group)
          next unless slot
          internal_name = "#{dest.name}##{source}"
          key = internal_name.dup.force_encoding(Encoding::ASCII_8BIT)
          # Internal names are rewritten, so they must never occupy an external
          # work order spelling; a collision means the disambiguation failed.
          claim(reserved, key, slot.slot)
          names_array << key
          names_array << [page_ref(slot.slot), *dest.view]
          @name_tree_rows << { kind: :internal, name: internal_name, original: dest.name,
                               source: source, slot: slot.slot, basis: basis, detail: detail }
        end
      end

      { Type: :Names, Dests: { Names: names_array } }
    end

    def claim(reserved, key, slot)
      if reserved.key?(key) && reserved[key] != slot
        raise "name tree key #{key.dup.force_encoding('UTF-8').inspect} already resolves to slot #{reserved[key]}, cannot also claim slot #{slot}"
      end
      reserved[key] = slot
    end

    def chapter_group(source)
      @manifest.slots.find { |slot| slot.source == source }.group
    end

    def normalized_view(view)
      view.empty? ? [:Fit] : view
    end
  end
end
