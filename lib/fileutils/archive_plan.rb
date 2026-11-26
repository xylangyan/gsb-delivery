# frozen_string_literal: true

require "json"

module FileUtils
  # A read-only pre-flight planner for the archive staging workflow.
  #
  # The planner never touches the working volume.  It reads a manifest (the
  # sign-off sheet plus the observed working-volume state) and returns an
  # ArchivePlan describing, group by group:
  #
  # - which preview attachment is downloaded,
  # - which preview names survive as staging hard links,
  # - which whole supply package is downloaded for a formal group,
  # - when staging names may be dropped,
  # - the attachment bytes occupied after every step and at the peak.
  #
  # Bytes are counted by physical attachment: several hard links to the same
  # item count once.  Names are treated as the same item only when the
  # manifest says so (see +aliases+ / +same_items+).
  #
  # == Manifest
  #
  # A manifest is a Hash.  String and symbol keys are both accepted.
  #
  #   {
  #     "quota" => 6,
  #     "bill_limit" => 9,                 # optional
  #     "attachments" => {"1" => 1, ...},  # name => size
  #     "aliases" => [["a", "b"], ...],    # names for the same item
  #     "initial" => ["1"],                # already on the volume
  #     "verification_size" => 3,          # optional single handoff
  #     "queues" => [
  #       {"name" => "preview", "kind" => "preview",
  #        "items" => ["1", "2", ...],
  #        "handoffs" => [{"verification_size" => 3}]},
  #       {"name" => "formal-a", "kind" => "formal",
  #        "packages" => [["3", "4"], ...]}
  #     ],
  #     "preconditions" => [
  #       {"group" => "formal-a/0", "after" => "handoff/0"}
  #     ]
  #   }
  #
  # +attachments+ may also be an array of <tt>{"name", "size"}</tt> hashes
  # or <tt>[name, size]</tt> pairs.  A name mentioned by a queue but absent
  # from +attachments+ gets +default_size+ (1 byte by default).
  #
  # A formal package may contain any number of names.  A handoff group may
  # be declared in a queue's +handoffs+ array; it appends to that queue
  # after the queue's preview items.  By default every preview is complete
  # before any handoff, and every handoff is complete (its temporary
  # attachment removed) before any formal group starts.
  #
  # Explicit +after+ dependencies may name group ids, queue names, or the
  # special values +all_previews+ / +previews+ and +all_handoffs+ /
  # +handoffs+.
  #
  # == Example
  #
  #   plan = FileUtils::ArchivePlanner.new(
  #     "quota" => 6,
  #     "attachments" => (1..6).to_h {|i| [i.to_s, 1] },
  #     "verification_size" => 3,
  #     "queues" => [
  #       {"name" => "preview", "kind" => "preview",
  #        "items" => %w[1 2 3 4 5 6]},
  #       {"name" => "a", "kind" => "formal",
  #        "packages" => [%w[3 4], %w[1 2], %w[5 6]]},
  #       {"name" => "b", "kind" => "formal",
  #        "packages" => [%w[6 1], %w[2 3], %w[4 5]]}
  #     ]
  #   ).plan
  #   plan.minimum_download_bytes # => 10
  #
  class ArchivePlan
    Step = Struct.new(
      :number, :phase, :queue, :group, :action, :items, :package,
      :billed_bytes, :occupancy_before, :occupancy_after, :notes,
      keyword_init: true
    ) do
      def to_h
        {
          "step" => number,
          "phase" => phase,
          "queue" => queue,
          "group" => group,
          "action" => action,
          "items" => items,
          "package" => package,
          "billed_bytes" => billed_bytes,
          "occupancy_before" => occupancy_before,
          "occupancy_after" => occupancy_after,
          "notes" => notes
        }
      end
    end

    attr_reader :manifest, :steps, :errors, :minimum_download_bytes,
                :peak_occupancy, :quota, :bill_limit, :retained_items,
                :preview_download_bytes, :formal_download_bytes, :statistics,
                :preparation_peak, :acceptance_peak, :final_occupancy

    def initialize(feasible:, steps:, errors:, manifest:, quota:, bill_limit:,
                   minimum_download_bytes:, peak_occupancy:, retained_items:,
                   preview_download_bytes:, formal_download_bytes:,
                   statistics: {}, preparation_peak: nil, acceptance_peak: nil,
                   final_occupancy: nil)
      @feasible = feasible
      @steps = steps
      @errors = errors
      @manifest = manifest
      @quota = quota
      @bill_limit = bill_limit
      @minimum_download_bytes = minimum_download_bytes
      @peak_occupancy = peak_occupancy
      @retained_items = retained_items
      @preview_download_bytes = preview_download_bytes
      @formal_download_bytes = formal_download_bytes
      @statistics = statistics
      @preparation_peak = preparation_peak
      @acceptance_peak = acceptance_peak
      @final_occupancy = final_occupancy
    end

    def feasible?
      @feasible
    end

    def to_h
      {
        "feasible" => feasible?,
        "quota" => quota,
        "bill_limit" => bill_limit,
        "minimum_download_bytes" => minimum_download_bytes,
        "peak_occupancy" => peak_occupancy,
        "preparation_peak" => preparation_peak,
        "acceptance_peak" => acceptance_peak,
        "final_occupancy" => final_occupancy,
        "preview_download_bytes" => preview_download_bytes,
        "formal_download_bytes" => formal_download_bytes,
        "retained_items" => retained_items,
        "errors" => errors,
        "statistics" => statistics,
        "steps" => steps.map(&:to_h)
      }
    end

    def to_json(*args)
      JSON.generate(to_h, *args)
    end

    def to_s
      lines = []
      lines << "archive plan: #{feasible? ? 'feasible' : 'INFEASIBLE'}"
      lines << "quota=#{quota} bytes, minimum download=#{minimum_download_bytes} bytes, peak=#{peak_occupancy} bytes"
      unless preparation_peak.nil?
        lines << "preparation peak=#{preparation_peak} bytes, acceptance peak=#{acceptance_peak} bytes, final occupancy=#{final_occupancy} bytes"
      end
      lines << "bill limit=#{bill_limit} bytes" unless bill_limit.nil?
      unless errors.empty?
        lines << "errors:"
        errors.each { |error| lines << "  - #{error}" }
      end
      unless steps.empty?
        lines << ""
        lines << "steps:"
        steps.each do |step|
          line = format("%3d %-12s", step.number, step.phase)
          line << " #{step.queue}" if step.queue
          line << " #{step.group}" if step.group
          line << " #{step.action}"
          line << " #{step.package.join(',')}" if step.package && !step.package.empty?
          line << " billed=#{step.billed_bytes}" if step.billed_bytes.to_i > 0
          line << " occupancy #{step.occupancy_before}->#{step.occupancy_after}"
          line << " (#{step.notes})" if step.notes && !step.notes.empty?
          lines << line
        end
      end
      lines.join("\n")
    end
  end

  class ArchivePlanner
    class Error < StandardError; end
    class Infeasible < Error; end

    DEFAULT_SIZE = 1
    DEFAULT_TIME_LIMIT = 8.0
    DEFAULT_MAX_STATES = 400_000

    Group = Struct.new(
      :id, :queue, :kind, :items, :after, :verification_size,
      keyword_init: true
    )

    Node = Struct.new(:cost, :peak, :pos, :held, :used, :parent, :action)

    def self.from_file(path, **options)
      text = File.read(path)
      data =
        if File.extname(path).downcase == ".json"
          JSON.parse(text)
        else
          begin
            require "yaml"
            YAML.safe_load(text, permitted_classes: [], aliases: false)
          rescue LoadError, StandardError
            JSON.parse(text)
          end
        end
      new(data, **options)
    end

    def initialize(manifest, default_size: DEFAULT_SIZE,
                   time_limit: DEFAULT_TIME_LIMIT,
                   max_states: DEFAULT_MAX_STATES)
      unless manifest.is_a?(Hash)
        raise ArgumentError, "manifest must be a Hash"
      end
      @manifest = stringify_keys(manifest)
      @default_size = default_size
      @time_limit = time_limit
      @max_states = max_states
      @errors = []
      normalize!
    end

    def plan
      return infeasible(@errors.dup) unless @errors.empty?

      quota = @quota
      return infeasible(["quota must be a non-negative integer"]) if quota.nil?

      preview_items = (@initial_items + @preview_groups.flat_map(&:items)).uniq
      preview_peak = preview_items.sum { |item| size_of(item) }
      if preview_peak > quota
        return infeasible(["preview phase needs #{preview_peak} bytes but quota is #{quota}"])
      end

      base_occupancy = occupancy(@initial_mask)
      formal = solve_formal(quota, base_occupancy)
      if formal.nil?
        return infeasible([
          "no formal schedule fits quota #{quota} with the declared queue order and prerequisites"
        ])
      end

      minimum = preview_download_bytes + formal[:cost]
      if @bill_limit && minimum > @bill_limit
        return infeasible(
          ["minimum supplier download is #{minimum} bytes, above bill limit #{@bill_limit}; not startable"],
          minimum_download_bytes: minimum
        )
      end

      build_steps(formal, minimum)
    rescue Infeasible => e
      infeasible([e.message])
    end

    private

    # ------------------------------------------------------------------
    # Manifest normalisation
    # ------------------------------------------------------------------

    def stringify_keys(value)
      case value
      when Hash
        value.each_with_object({}) do |(key, val), out|
          out[key.to_s] = stringify_keys(val)
        end
      when Array
        value.map { |val| stringify_keys(val) }
      else
        value
      end
    end

    def fetch_value(hash, keys)
      keys.each { |key| return hash[key] if hash.key?(key) }
      nil
    end

    def fetch_int(hash, keys)
      value = fetch_value(hash, keys)
      return nil if value.nil?
      Integer(value)
    rescue ArgumentError, TypeError
      @errors << "#{keys.first} must be an integer"
      nil
    end

    def normalize!
      @attachments = {}
      @canonical = {}
      @canonical_sizes = {}
      @display = {}
      @groups = []
      @queues = []
      @preview_groups = []
      @handoff_groups = []
      @formal_queues = []

      @quota = fetch_int(@manifest, %w[quota space space_quota capacity limit])
      @bill_limit = fetch_int(@manifest, %w[bill_limit download_limit supplier_limit bill_cap])

      parse_attachments
      parse_aliases
      @initial_items = Array(fetch_value(@manifest, %w[initial held working_volume present]))
        .flatten.map { |name| canonical(name) }.uniq

      parse_queues
      parse_top_level_groups
      parse_top_level_handoffs
      parse_preconditions
      build_indexes
      @initial_mask = mask(@initial_items)
    end

    def parse_attachments
      raw = fetch_value(@manifest, %w[attachments items files])
      case raw
      when nil
        nil
      when Hash
        raw.each do |name, value|
          if value.is_a?(Hash) && !value.key?("size") && !value.key?("bytes")
            # Size is supplied by another name in the same alias group.
            next
          end
          add_attachment(name.to_s, attachment_size(value))
        end
      when Array
        raw.each do |entry|
          case entry
          when Hash
            name = entry["name"] || entry["id"] || entry["item"]
            add_attachment(name.to_s, attachment_size(entry["size"] || entry["bytes"])) if name
          when Array
            add_attachment(entry[0].to_s, attachment_size(entry[1])) if entry[0]
          else
            add_attachment(entry.to_s, @default_size)
          end
        end
      else
        @errors << "attachments must be a Hash or an Array"
      end
    end

    def attachment_size(value)
      case value
      when Hash
        Integer(value["size"] || value["bytes"] || @default_size)
      when nil
        @default_size
      else
        Integer(value)
      end
    rescue ArgumentError, TypeError
      @errors << "invalid attachment size #{value.inspect}"
      @default_size
    end

    def add_attachment(name, size)
      return if name.nil? || name.empty?
      if @attachments.key?(name) && @attachments[name] != size
        @errors << "attachment #{name} has conflicting sizes"
      end
      @attachments[name] ||= size
    end

    def parse_aliases
      groups = []
      raw = fetch_value(@manifest, %w[aliases same_items reusable reuse same_as])
      case raw
      when Array
        raw.each do |entry|
          case entry
          when Array
            groups << entry.map(&:to_s)
          when Hash
            names = entry["names"] || entry["items"] || entry["same"]
            groups << Array(names).map(&:to_s) if names
          end
        end
      when Hash
        raw.each_value { |names| groups << Array(names).map(&:to_s) }
      end

      attachments = @manifest["attachments"]
      if attachments.is_a?(Hash)
        attachments.each do |name, value|
          next unless value.is_a?(Hash)
          same = value["same_as"] || value["same"] || value["alias_of"]
          groups << ([name.to_s] + Array(same).map(&:to_s)) if same
        end
      end

      groups = groups.map(&:uniq).reject { |group| group.length < 2 }

      # An alias name inherits the size declared for any member of its group;
      # only a group with no declared size falls back to the default.
      sizes_by_name = @attachments.dup
      groups.each do |group|
        declared = group.map { |name| sizes_by_name[name] }.compact.uniq
        if declared.length > 1
          @errors << "aliased names for #{group.first} have different sizes"
        end
        size = declared.first || @default_size
        group.each { |name| @attachments[name] = size unless @attachments.key?(name) }
      end

      parent = {}
      find = lambda do |name|
        parent[name] ||= name
        while parent[name] != name
          parent[name] = parent[parent[name]]
          name = parent[name]
        end
        name
      end
      union = lambda do |a, b|
        ra = find.call(a)
        rb = find.call(b)
        parent[rb] = ra unless ra == rb
      end

      groups.each do |group|
        group[1..-1].each { |name| union.call(group[0], name) }
      end

      roots = {}
      @attachments.each do |name, size|
        root = find.call(name)
        if roots.key?(root) && roots[root] != size
          @errors << "aliased names for #{root} have different sizes"
        end
        roots[root] ||= size
        @canonical[name] = root
        @canonical_sizes[root] = size
        @display[root] ||= name
      end
    end

    def canonical(name)
      name = name.to_s
      return @canonical[name] if @canonical.key?(name)
      add_attachment(name, @default_size)
      @canonical[name] = name
      @canonical_sizes[name] ||= @attachments[name] || @default_size
      @display[name] ||= name
      name
    end

    def size_of(item)
      @canonical_sizes[item] || @attachments[item] || @default_size
    end

    def parse_queues
      raw = fetch_value(@manifest, %w[queues queue_list])
      raw = [] if raw.nil?
      if raw.is_a?(Hash)
        raw = raw.map do |key, value|
          value = stringify_keys(value)
          value.is_a?(Hash) ? {"name" => key.to_s}.merge(value) : value
        end
      end

      Array(raw).each_with_index do |entry, index|
        entry = stringify_keys(entry)
        name = (entry["name"] || entry["id"] || "queue-#{index}").to_s
        kind = (entry["kind"] || entry["type"] || infer_kind(entry)).to_s
        queue = {"name" => name, "groups" => []}

        # Preview items.
        if entry.key?("items") && !entry.key?("packages")
          if kind == "formal"
            add_package(queue, entry, name, 0)
          else
            Array(entry["items"]).each do |item|
              item = stringify_keys(item)
              item_name = item.is_a?(Hash) ? (item["name"] || item["id"] || item["item"]) : item
              next if item_name.nil?
              add_preview(queue, canonical(item_name), name,
                          item.is_a?(Hash) ? item["after"] : nil,
                          item.is_a?(Hash) ? item["id"] : nil)
            end
          end
        end

        # Formal packages.
        if entry.key?("packages")
          Array(entry["packages"]).each_with_index do |package, pi|
            add_package(queue, package, name, pi)
          end
        elsif kind == "formal" && entry.key?("groups")
          Array(entry["groups"]).each_with_index do |package, pi|
            add_package(queue, package, name, pi)
          end
        end

        # Handoff groups appended to this queue.
        if entry.key?("handoffs")
          Array(entry["handoffs"]).each_with_index do |handoff, hi|
            add_handoff(queue, handoff, name, hi)
          end
        elsif kind == "handoff" && entry.key?("groups")
          Array(entry["groups"]).each_with_index do |handoff, hi|
            add_handoff(queue, handoff, name, hi)
          end
        end

        # Generic per-group declarations.
        if entry.key?("groups") && !entry.key?("packages") && kind != "formal" && kind != "handoff"
          Array(entry["groups"]).each_with_index do |group, gi|
            group = stringify_keys(group)
            group_kind = (group["kind"] || group["type"] || "formal").to_s
            case group_kind
            when "preview", "qc", "quality"
              items = Array(group["items"] || group["item"] || group["name"])
              items.each_with_index do |item, ii|
                add_preview(queue, canonical(item), name, group["after"],
                            group["id"] || "#{name}/#{gi}/#{ii}")
              end
            when "handoff", "verification", "verify"
              add_handoff(queue, group, name, gi)
            else
              add_package(queue, group, name, gi)
            end
          end
        end

        @queues << queue unless queue["groups"].empty?
      end
    end

    def infer_kind(entry)
      return "preview" if entry.key?("items") && !entry.key?("packages")
      return "handoff" if entry.key?("handoffs") || entry.key?("verification_size")
      "formal"
    end

    def add_preview(queue, item, queue_name, after = nil, id = nil)
      group = Group.new(
        id: (id || "#{queue_name}/preview/#{queue['groups'].count { |g| g.kind == :preview }}").to_s,
        queue: queue_name, kind: :preview, items: [item],
        after: Array(after).map(&:to_s), verification_size: nil
      )
      queue["groups"] << group
      @groups << group
      @preview_groups << group
    end

    def add_package(queue, package, queue_name, index)
      package = stringify_keys(package)
      if package.is_a?(Hash)
        names = package["items"] || package["package"] || package["attachments"] || package["names"]
        id = package["id"] || package["name"]
        after = package["after"] || package["preconditions"] || package["requires"]
      else
        names = package
        id = nil
        after = nil
      end
      names = Array(names)
      return if names.empty?
      group = Group.new(
        id: (id || "#{queue_name}/#{index}").to_s,
        queue: queue_name, kind: :formal,
        items: names.map { |item| canonical(item) }.uniq,
        after: Array(after).map(&:to_s), verification_size: nil
      )
      queue["groups"] << group
      @groups << group
    end

    def add_handoff(queue, handoff, queue_name, index)
      handoff = stringify_keys(handoff)
      if handoff.is_a?(Hash)
        size = handoff["verification_size"] || handoff["size"] || handoff["bytes"]
        id = handoff["id"] || handoff["name"]
        after = handoff["after"] || handoff["preconditions"] || handoff["requires"]
        items = handoff["items"] || handoff["attachments"]
      else
        size = handoff
        id = nil
        after = nil
        items = nil
      end
      return if size.nil?
      group = Group.new(
        id: (id || "#{queue_name}/handoff/#{index}").to_s,
        queue: queue_name, kind: :handoff,
        items: Array(items).map { |item| canonical(item) },
        after: Array(after).map(&:to_s),
        verification_size: Integer(size)
      )
      queue["groups"] << group
      @groups << group
      @handoff_groups << group
    rescue ArgumentError, TypeError
      @errors << "invalid verification size for #{queue_name}/#{index}"
    end

    def parse_top_level_groups
      raw = fetch_value(@manifest, %w[groups group_list])
      return if raw.nil?
      Array(raw).each_with_index do |entry, index|
        entry = stringify_keys(entry)
        kind = (entry["kind"] || entry["type"] || "formal").to_s
        queue_name = (entry["queue"] || "group-#{index}").to_s
        queue = @queues.find { |q| q["name"] == queue_name }
        unless queue
          queue = {"name" => queue_name, "groups" => []}
          @queues << queue
        end
        case kind
        when "preview", "qc", "quality"
          Array(entry["items"] || entry["item"] || entry["name"]).each_with_index do |item, ii|
            add_preview(queue, canonical(item), queue_name, entry["after"],
                        entry["id"] ? "#{entry['id']}/#{ii}" : nil)
          end
        when "handoff", "verification", "verify"
          add_handoff(queue, entry, queue_name, index)
        else
          add_package(queue, entry, queue_name, index)
        end
      end
    end

    def parse_top_level_handoffs
      raw = fetch_value(@manifest, %w[handoffs verification_groups])
      if raw
        queue_name = "handoffs"
        queue = @queues.find { |q| q["name"] == queue_name }
        unless queue
          queue = {"name" => queue_name, "groups" => []}
          @queues << queue
        end
        Array(raw).each_with_index { |handoff, i| add_handoff(queue, handoff, queue_name, i) }
      end

      return unless @handoff_groups.empty?
      size = fetch_int(@manifest, %w[verification_size temp_size temporary_size])
      return if size.nil?
      queue_name = (@queues.first && @queues.first["name"]) || "verification"
      queue = @queues.find { |q| q["name"] == queue_name }
      unless queue
        queue = {"name" => queue_name, "groups" => []}
        @queues << queue
      end
      add_handoff(queue, {"id" => "verification/0", "verification_size" => size}, queue_name, 0)
    end

    def parse_preconditions
      @explicit_after = Hash.new { |hash, key| hash[key] = [] }
      raw = fetch_value(@manifest, %w[preconditions dependencies requires])
      case raw
      when Array
        raw.each do |entry|
          entry = stringify_keys(entry)
          group = entry["group"] || entry["id"] || entry["name"]
          after = entry["after"] || entry["requires"] || entry["preconditions"]
          next if group.nil?
          @explicit_after[group.to_s].concat(Array(after).map(&:to_s))
        end
      when Hash
        raw.each { |group, after| @explicit_after[group.to_s].concat(Array(after).map(&:to_s)) }
      end
      @groups.each do |group|
        @explicit_after[group.id].concat(group.after)
        group.after = (@explicit_after[group.id] || []).uniq
      end
    end

    def build_indexes
      @item_order = (@initial_items + @groups.flat_map(&:items)).uniq
      @item_index = {}
      @item_order.each_with_index { |item, index| @item_index[item] = index }
      @preview_mask = mask(@preview_groups.flat_map(&:items))
      @group_by_id = {}
      @groups.each do |group|
        @errors << "duplicate group id #{group.id}" if @group_by_id.key?(group.id)
        @group_by_id[group.id] = group
      end

      @preview_queues = []
      @queues.each do |queue|
        groups = queue["groups"].select { |g| g.kind == :preview }
        @preview_queues << {"name" => queue["name"], "groups" => groups} unless groups.empty?
      end

      @formal_queues = []
      @queues.each do |queue|
        groups = queue["groups"].select { |g| g.kind == :formal }
        @formal_queues << {"name" => queue["name"], "groups" => groups} unless groups.empty?
      end

      @package_masks = {}
      @package_costs = {}
      @groups.select { |g| g.kind == :formal }.each do |group|
        group.items = group.items.uniq
        @package_masks[group.id] = mask(group.items)
        @package_costs[group.id] = group.items.sum { |item| size_of(item) }
      end

      # Validate prerequisites.  The formal search rebuilds the actual masks
      # against its own global group indices.
      formal_ids = @formal_queues.flat_map { |queue| queue["groups"].map(&:id) }
      @formal_queues.each do |queue|
        queue["groups"].each do |group|
          @explicit_after[group.id].each do |dep|
            next if %w[all_previews previews all_preview
                       all_handoffs handoffs verification].include?(dep)
            next if formal_ids.include?(dep)
            next if @queues.any? { |q| q["name"] == dep }
            next if @group_by_id.key?(dep)
            @errors << "unknown prerequisite #{dep.inspect} for #{group.id}"
          end
        end
      end
    end

    # ------------------------------------------------------------------
    # Accounting
    # ------------------------------------------------------------------

    def preview_download_bytes
      seen = {}
      @initial_items.each { |item| seen[item] = true }
      total = 0
      @preview_groups.each do |group|
        group.items.each do |item|
          next if seen[item]
          seen[item] = true
          total += size_of(item)
        end
      end
      total
    end

    def mask(items)
      items.inject(0) do |bits, item|
        index = @item_index[item]
        index ? (bits | (1 << index)) : bits
      end
    end

    def occupancy(bits)
      @occupancy_cache ||= {}
      @occupancy_cache[bits] ||= begin
        total = 0
        remaining = bits
        while remaining != 0
          low = remaining & -remaining
          item = @item_order[low.bit_length - 1]
          total += size_of(item) if item
          remaining ^= low
        end
        total
      end
    end

    def bits_to_items(bits)
      items = []
      remaining = bits
      while remaining != 0
        low = remaining & -remaining
        item = @item_order[low.bit_length - 1]
        items << item if item
        remaining ^= low
      end
      items
    end

    # ------------------------------------------------------------------
    # Formal-stage optimiser
    # ------------------------------------------------------------------

    def solve_formal(quota, base_occupancy)
      if @formal_queues.empty?
        return {cost: 0, path: [], retained: 0, peak: base_occupancy}
      end

      max_temp = @handoff_groups.map { |g| g.verification_size.to_i }.max.to_i
      cap = quota - max_temp - base_occupancy
      return nil if cap < 0

      queue_groups = @formal_queues.map { |q| q["groups"] }
      lengths = queue_groups.map(&:length)
      group_objects = queue_groups.flatten
      group_bits = {}
      group_objects.each_with_index { |group, index| group_bits[group.id] = index }

      pre_masks = Array.new(group_objects.length, 0)
      @formal_queues.each do |queue|
        queue["groups"].each do |group|
          bit = group_bits[group.id]
          @explicit_after[group.id].each do |dep|
            if group_bits.key?(dep)
              pre_masks[bit] |= 1 << group_bits[dep]
            elsif (dep_queue = @queues.find { |q| q["name"] == dep })
              dep_queue["groups"].each do |g|
                pre_masks[bit] |= 1 << group_bits[g.id] if group_bits.key?(g.id)
              end
            end
          end
        end
      end

      heap = MinHeap.new
      seen = {}
      seq = 0

      start = Node.new(0, base_occupancy, zeros(lengths.length), @initial_mask, 0, nil, nil)
      heap.push([0, base_occupancy, 0, seq], start)
      seq += 1

      best = nil
      visited = 0
      deadline = Time.now + @time_limit

      while (entry = heap.pop)
        node = entry.last
        visited += 1
        break if visited > @max_states
        break if (visited % 256).zero? && Time.now > deadline

        key = node.pos.join(",")
        list = (seen[key] ||= [])
        next if dominated?(list, node)
        add_frontier(list, node)

        if node.pos == lengths
          best = node
          break
        end

        lengths.each_with_index do |len, qi|
          pi = node.pos[qi]
          next if pi >= len
          group = queue_groups[qi][pi]
          bit = group_bits[group.id]
          next unless (pre_masks[bit] & completed_mask(node.pos, queue_groups, group_bits)) == pre_masks[bit]

          package_mask = @package_masks[group.id]
          missing = package_mask & ~node.held
          new_pos = node.pos.dup
          new_pos[qi] += 1

          if missing.zero?
            child = Node.new(node.cost, node.peak, new_pos, node.held, node.used,
                             node, [:free, qi, pi, group])
            push_node(heap, child, seq)
            seq += 1
            next
          end

          new_held = node.held | package_mask
          new_occ = occupancy(new_held)
          if new_occ <= quota
            child = Node.new(node.cost + @package_costs[group.id],
                             [node.peak, new_occ].max, new_pos, new_held,
                             node.used, node, [:download, qi, pi, group])
            push_node(heap, child, seq)
            seq += 1
          end

          if (missing & ~@preview_mask).zero?
            add = occupancy(missing)
            if node.used + add <= cap
              new_used = node.used + add
              retained_held = node.held | missing
              retained_occ = occupancy(retained_held)
              if retained_occ <= quota
                child = Node.new(node.cost, [node.peak, retained_occ].max,
                                 new_pos, retained_held, new_used, node,
                                 [:retain, qi, pi, group])
                push_node(heap, child, seq)
                seq += 1
              end
            end
          end
        end
      end

      return nil unless best

      path = []
      node = best
      while node.parent
        path.unshift(node.action)
        node = node.parent
      end

      # Replay the selected actions to recover the exact set of preview
      # items that must be staged before the preview names are removed.
      # A :retain action only needs the items missing at that moment; items
      # already downloaded are not staging choices.
      retained = 0
      held = @initial_mask
      path.each do |action|
        kind, _qi, _pi, group = action
        package_mask = @package_masks[group.id]
        case kind
        when :retain
          missing = package_mask & ~held
          retained |= (missing & @preview_mask)
          held |= missing
        when :download
          held |= package_mask
        end
      end

      {cost: best.cost, path: path, retained: retained, peak: best.peak}
    end

    def zeros(length)
      Array.new(length, 0)
    end

    def completed_mask(pos, queue_groups, group_bits)
      bits = 0
      queue_groups.each_with_index do |groups, qi|
        groups[0, pos[qi]].each { |group| bits |= 1 << group_bits[group.id] }
      end
      bits
    end

    def dominated?(list, node)
      list.any? do |held, used, cost, peak|
        cost <= node.cost && used <= node.used &&
          (node.held & ~held).zero? && peak <= node.peak
      end
    end

    def add_frontier(list, node)
      list.reject! do |held, used, cost, peak|
        node.cost <= cost && node.used <= used &&
          (held & ~node.held).zero? && node.peak <= peak
      end
      list << [node.held, node.used, node.cost, node.peak]
    end

    def push_node(heap, node, seq)
      heap.push([node.cost, node.peak, node.used, seq], node)
    end

    # ------------------------------------------------------------------
    # Plan construction
    # ------------------------------------------------------------------

    def build_steps(formal, minimum)
      steps = []
      number = 0
      held = @initial_mask
      occupancy_now = occupancy(held)
      peak = occupancy_now

      # Preview groups.  Interleave preview queues while preserving each
      # queue's local order and explicit dependencies.
      done = {}
      cursors = Array.new(@preview_queues.length, 0)
      preview_seen = {}
      @initial_items.each { |item| preview_seen[item] = true }
      preview_names = []

      loop do
        progressed = false
        @preview_queues.each_with_index do |queue, qi|
          group = queue["groups"][cursors[qi]]
          next if group.nil?
          next unless preview_ready?(group, done)
          cursors[qi] += 1
          done[group.id] = true
          progressed = true
          item = group.items.first
          before = occupancy_now

          if preview_seen[item]
            action = "reuse_preview"
            billed = 0
            note = "attach preview name to already-present #{display(item)}"
          else
            preview_seen[item] = true
            action = "download_preview"
            billed = size_of(item)
            held |= mask([item])
            occupancy_now += billed
            peak = [peak, occupancy_now].max
            note = "download #{display(item)} (#{billed} byte#{billed == 1 ? '' : 's'}) and attach preview name"
          end
          preview_names << item
          steps << ArchivePlan::Step.new(
            number: (number += 1), phase: "preview", queue: queue["name"],
            group: group.id, action: action, items: [display(item)],
            package: nil, billed_bytes: billed, occupancy_before: before,
            occupancy_after: occupancy_now, notes: note
          )
        end
        break unless progressed
      end

      if @preview_groups.any? { |group| !done[group.id] }
        raise Infeasible, "preview queues contain a cycle or an unsatisfiable prerequisite"
      end

      retained_items = bits_to_items(formal[:retained] & @preview_mask)
      retained_held = @initial_mask | mask(retained_items)
      after_removal = occupancy(retained_held)
      preparation_peak = peak
      stage_note =
        if retained_items.empty?
          "remove all preview names; no staging hard links retained"
        else
          "create staging hard links for #{retained_items.map { |i| display(i) }.join(',')}; remove all preview names"
        end
      steps << ArchivePlan::Step.new(
        number: (number += 1), phase: "staging", queue: nil, group: nil,
        action: "stage_and_remove_previews", items: retained_items.map { |i| display(i) },
        package: nil, billed_bytes: 0, occupancy_before: occupancy_now,
        occupancy_after: after_removal, notes: stage_note
      )
      held = retained_held
      occupancy_now = after_removal
      peak = [peak, occupancy_now].max

      # Handoff / verification groups, in declaration order.
      @handoff_groups.each do |group|
        temp = group.verification_size.to_i
        before = occupancy_now
        occupancy_now += temp
        if occupancy_now > @quota
          raise Infeasible,
                "verification group #{group.id} needs #{occupancy_now} bytes but quota is #{@quota}"
        end
        peak = [peak, occupancy_now].max
        steps << ArchivePlan::Step.new(
          number: (number += 1), phase: "verification", queue: group.queue,
          group: group.id, action: "add_verification", items: [],
          package: nil, billed_bytes: 0, occupancy_before: before,
          occupancy_after: occupancy_now,
          notes: "create #{temp}-byte temporary verification attachment"
        )
        steps << ArchivePlan::Step.new(
          number: (number += 1), phase: "verification", queue: group.queue,
          group: group.id, action: "remove_verification", items: [],
          package: nil, billed_bytes: 0, occupancy_before: occupancy_now,
          occupancy_after: before,
          notes: "verification complete; remove temporary attachment"
        )
        occupancy_now = before
      end

      # Formal groups in the optimiser's interleaving order.
      formal_peak = occupancy_now
      formal_cost = 0
      staging_remaining = retained_items.dup
      formal[:path].each do |action|
        kind, qi, _pi, group = action
        package_items = group.items.uniq
        package_names = package_items.map { |item| display(item) }
        before = occupancy_now

        case kind
        when :free
          steps << ArchivePlan::Step.new(
            number: (number += 1), phase: "formal",
            queue: @formal_queues[qi]["name"], group: group.id,
            action: "attach_formal", items: package_names,
            package: nil, billed_bytes: 0,
            occupancy_before: before, occupancy_after: before,
            notes: "all package attachments already held; attach formal names"
          )
        when :retain
          missing = package_items.select { |item| (held & mask([item])) == 0 }
          missing.each { |item| held |= mask([item]) }
          occupancy_now = occupancy(held)
          released = package_items.select { |item| staging_remaining.include?(item) }
          staging_remaining -= released
          note = "use staging hard links; attach formal names"
          unless released.empty?
            note += "; drop staging name#{released.length == 1 ? '' : 's'} " \
                    "#{released.map { |i| display(i) }.join(',')} (formal name remains)"
          end
          steps << ArchivePlan::Step.new(
            number: (number += 1), phase: "formal",
            queue: @formal_queues[qi]["name"], group: group.id,
            action: "attach_formal_from_staging", items: package_names,
            package: package_names, billed_bytes: 0,
            occupancy_before: before, occupancy_after: occupancy_now,
            notes: note
          )
        when :download
          billed = @package_costs[group.id]
          held |= @package_masks[group.id]
          occupancy_now = occupancy(held)
          formal_cost += billed
          released = package_items.select { |item| staging_remaining.include?(item) }
          staging_remaining -= released
          note = "download whole package (#{billed} bytes billed); attach formal names"
          unless released.empty?
            note += "; drop staging name#{released.length == 1 ? '' : 's'} " \
                    "#{released.map { |i| display(i) }.join(',')} (formal name remains)"
          end
          steps << ArchivePlan::Step.new(
            number: (number += 1), phase: "formal",
            queue: @formal_queues[qi]["name"], group: group.id,
            action: "download_package", items: package_names,
            package: package_names, billed_bytes: billed,
            occupancy_before: before, occupancy_after: occupancy_now,
            notes: note
          )
        end
        formal_peak = [formal_peak, occupancy_now].max
        peak = [peak, formal_peak].max
      end

      steps << ArchivePlan::Step.new(
        number: (number += 1), phase: "close", queue: nil, group: nil,
        action: "close_volume", items: [], package: nil, billed_bytes: 0,
        occupancy_before: occupancy_now, occupancy_after: 0,
        notes: "all formal groups accepted; collect formal names"
      )
      peak = [peak, formal_peak].max

      minimum = preview_download_bytes + formal_cost
      if @bill_limit && minimum > @bill_limit
        return infeasible(
          ["minimum supplier download is #{minimum} bytes, above bill limit #{@bill_limit}; not startable"],
          minimum_download_bytes: minimum
        )
      end

      ArchivePlan.new(
        feasible: true, steps: steps, errors: [],
        manifest: @manifest, quota: @quota, bill_limit: @bill_limit,
        minimum_download_bytes: minimum, peak_occupancy: peak,
        retained_items: retained_items.map { |item| display(item) },
        preview_download_bytes: preview_download_bytes,
        formal_download_bytes: formal_cost,
        preparation_peak: preparation_peak,
        acceptance_peak: formal_peak,
        final_occupancy: occupancy_now,
        statistics: {
          "preview_groups" => @preview_groups.length,
          "formal_groups" => @groups.count { |g| g.kind == :formal },
          "handoff_groups" => @handoff_groups.length,
          "steps" => steps.length
        }
      )
    end

    def preview_ready?(group, done)
      group.after.all? do |dep|
        dep == "all_previews" || dep == "previews" ||
          done[dep] ||
          ((queue = @queues.find { |q| q["name"] == dep }) &&
           queue["groups"].all? { |g| done[g.id] })
      end
    end

    def display(item)
      @display[item] || item.to_s
    end

    def infeasible(errors, minimum_download_bytes: nil)
      ArchivePlan.new(
        feasible: false, steps: [], errors: Array(errors).compact,
        manifest: @manifest, quota: @quota, bill_limit: @bill_limit,
        minimum_download_bytes: minimum_download_bytes,
        peak_occupancy: nil, retained_items: [],
        preview_download_bytes: nil, formal_download_bytes: nil,
        statistics: {}
      )
    end

    class MinHeap
      def initialize
        @items = []
      end

      def push(key, value)
        @items << [key, value]
        sift_up(@items.length - 1)
      end

      def pop
        return nil if @items.empty?
        top = @items[0]
        last = @items.pop
        unless @items.empty?
          @items[0] = last
          sift_down(0)
        end
        top
      end

      private

      def sift_up(index)
        while index > 0
          parent = (index - 1) / 2
          break if (@items[parent][0] <=> @items[index][0]) <= 0
          @items[parent], @items[index] = @items[index], @items[parent]
          index = parent
        end
      end

      def sift_down(index)
        loop do
          left = index * 2 + 1
          right = left + 1
          smallest = index
          if left < @items.length && (@items[left][0] <=> @items[smallest][0]) < 0
            smallest = left
          end
          if right < @items.length && (@items[right][0] <=> @items[smallest][0]) < 0
            smallest = right
          end
          break if smallest == index
          @items[index], @items[smallest] = @items[smallest], @items[index]
          index = smallest
        end
      end
    end
  end

  # Plans an archive run from a manifest Hash without touching the volume.
  def plan_archive(manifest, **options)
    ArchivePlanner.new(manifest, **options).plan
  end
  module_function :plan_archive

  alias archive_plan plan_archive
  module_function :archive_plan
end
