#
# HotParseConfig
# ==============
#
# Incrementally reconfigurable sibling of ParseConfig.  A HotParseConfig
# starts by reading a complete rules document (exactly like ParseConfig)
# and afterwards applies "batches" of line edits - insertions, deletions
# and replacements, all positioned against the pre-edit document - that an
# operator console publishes as a single release.
#
# A batch is validated as a whole: the collector keeps serving the current
# version while the candidate document is checked, and the switch to the
# new version is one atomic reference swap.  If any rule of the candidate
# is unreadable (for example a global flag and a group header that share
# the same name), the whole batch is rejected with a ConflictError that
# points at the offending physical lines of the candidate document.
#
# After each publish, key lookups, value types and group order are
# guaranteed to match a fresh ParseConfig read of the new document using
# the same separator and comment settings.  Lookups can additionally
# report the physical line number (in the current document) of the rule
# that produced a value.
#
# Internally the document and every index are persistent (immutable) data
# structures, so publishing a batch costs proportionally to the size of
# the batch - not to the size of the document - and readers of the current
# version are never disturbed.  Renaming a group header re-files the whole
# section (even one with a hundred thousand rules) without scanning or
# copying its lines.
#

class HotParseConfig
  # Raised when a published batch would produce a document that a plain
  # ParseConfig could not read (for example a global flag and a group
  # header sharing the same name).  The message carries the physical
  # line numbers of the conflicting rules in the candidate document.
  class ConflictError < ArgumentError; end

  # Persistent (immutable) ordered map implemented as a randomized treap
  # with subtree sizes.  Besides the usual O(log n) get/set/delete it
  # supports order statistics: rank (key -> position) and select
  # (position -> key), which the document uses to translate between
  # physical line numbers and stable line keys.
  class Treap
    MAX_PRIORITY = 1 << 62

    Node = Struct.new(:key, :value, :priority, :left, :right) do
      def size
        1 + (left ? left.size : 0) + (right ? right.size : 0)
      end
    end

    def self.node_size(node)
      node ? node.size : 0
    end

    def self.merge(left, right)
      return left unless right
      return right unless left

      if left.priority < right.priority
        Node.new(left.key, left.value, left.priority, left.left,
                 merge(left.right, right))
      else
        Node.new(right.key, right.value, right.priority,
                 merge(left, right.left), right.right)
      end
    end

    # Splits into [keys < split_key, keys >= split_key].
    def self.split(node, split_key)
      return [nil, nil] unless node

      if split_key <= node.key
        low, high = split(node.left, split_key)
        [low, Node.new(node.key, node.value, node.priority, high, node.right)]
      else
        low, high = split(node.right, split_key)
        [Node.new(node.key, node.value, node.priority, node.left, low), high]
      end
    end

    # Splits into [keys <= split_key, keys > split_key].
    def self.split_le(node, split_key)
      return [nil, nil] unless node

      if split_key < node.key
        low, high = split_le(node.left, split_key)
        [low, Node.new(node.key, node.value, node.priority, high, node.right)]
      else
        low, high = split_le(node.right, split_key)
        [Node.new(node.key, node.value, node.priority, node.left, low), high]
      end
    end

    attr_reader :root

    def initialize(root = nil)
      @root = root
    end

    def empty?
      @root.nil?
    end

    def size
      Treap.node_size(@root)
    end

    def get(key)
      node = @root
      while node
        if key < node.key
          node = node.left
        elsif key > node.key
          node = node.right
        else
          return node.value
        end
      end
      nil
    end

    def key?(key)
      !get(key).nil?
    end

    def set(key, value)
      low, rest = Treap.split(@root, key)
      _equal, high = Treap.split_le(rest, key)
      node = Node.new(key, value, rand(MAX_PRIORITY), nil, nil)
      Treap.new(Treap.merge(Treap.merge(low, node), high))
    end

    def delete(key)
      low, rest = Treap.split(@root, key)
      _equal, high = Treap.split_le(rest, key)
      Treap.new(Treap.merge(low, high))
    end

    # Splits into [treap with keys < split_key, treap with keys >= split_key].
    def split(split_key)
      low, high = Treap.split(@root, split_key)
      [Treap.new(low), Treap.new(high)]
    end

    # Number of keys strictly less than key.
    def rank(key)
      node = @root
      count = 0
      while node
        if key <= node.key
          node = node.left
        else
          count += Treap.node_size(node.left) + 1
          node = node.right
        end
      end
      count
    end

    # Key at zero-based position index, or nil when out of range.
    def select_key(index)
      node = @root
      while node
        left_size = Treap.node_size(node.left)
        if index < left_size
          node = node.left
        elsif index == left_size
          return node.key
        else
          index -= left_size + 1
          node = node.right
        end
      end
      nil
    end

    def min_key
      node = @root
      node = node.left while node && node.left
      node && node.key
    end

    def max_key
      node = @root
      node = node.right while node && node.right
      node && node.key
    end

    # Greatest key <= key, or nil.
    def floor_key(key)
      node = @root
      best = nil
      while node
        if node.key <= key
          best = node.key
          node = node.right
        else
          node = node.left
        end
      end
      best
    end

    # Smallest key > key, or nil.
    def succ_key(key)
      node = @root
      best = nil
      while node
        if node.key > key
          best = node.key
          node = node.left
        else
          node = node.right
        end
      end
      best
    end

    def each(&block)
      return enum_for(:each) unless block

      Treap.each_node(@root, &block)
      self
    end

    def reverse_each(&block)
      return enum_for(:reverse_each) unless block

      Treap.reverse_each_node(@root, &block)
      self
    end

    def self.each_node(node, &block)
      return unless node

      each_node(node.left, &block)
      block.call(node.key, node.value)
      each_node(node.right, &block)
    end

    def self.reverse_each_node(node, &block)
      return unless node

      reverse_each_node(node.right, &block)
      block.call(node.key, node.value)
      reverse_each_node(node.left, &block)
    end
  end

  EMPTY_TREAP = Treap.new

  # A single rule occurrence: its parsed value and the stable key of the
  # document line it came from.
  Entry = Struct.new(:value, :line_key)

  # The rules of one document section, indexed both by parameter name
  # (for key lookups; every occurrence is kept so deleting the latest
  # write restores the previous one) and by line key (for section splits).
  Section = Struct.new(:by_name, :by_line)

  # An immutable snapshot of the whole document plus its indexes.
  Version = Struct.new(
    :lines,           # Treap: line key => stripped raw line
    :headers,         # Treap: line key => group name (live headers only)
    :headers_by_name, # Treap: group name => Treap(line key => true)
    :sections,        # Treap: section id (creating header key) => Section
    :governor,        # Treap: section id => governing header line key
    :by_governor,     # Treap: header line key => Treap(section id => true)
    :groups_map,      # Treap: group name => Treap(section id => true)
    :global_params,   # Treap: name => Treap(line key => value) (params before the first header)
    :flags            # Treap: name => Treap(line key => true) (bare flags)
  ) do
    def with(changes)
      copy = dup
      changes.each { |field, value| copy[field] = value }
      copy
    end
  end

  attr_reader :config_file, :separator, :comments

  def initialize(config_file = nil, separator = '=', comments = ['#', ';'])
    @config_file = config_file
    @separator = separator
    @comments = comments
    @split_regex = '\s*' + separator + '\s*'
    @lock = Mutex.new
    @version = empty_version
    import_config if config_file
  end

  # Validate the config file, and contents (same contract as ParseConfig).
  def validate_config
    return if File.readable?(config_file)

    raise Errno::EACCES, "#{config_file} is not readable"
  end

  # Read the complete rules document, exactly like ParseConfig does.
  def import_config
    validate_config
    version = empty_version
    touched = {}
    File.foreach(config_file).with_index do |raw, index|
      line = raw.strip
      if index.zero?
        begin
          bom = "\xef\xbb\xbf".force_encoding('UTF-8')
          line = line.delete(bom) if line.include?(bom)
        rescue NoMethodError
        end
      end
      version = version.with(lines: version.lines.set(index, line))
      version = add_line(version, index, line, touched)
    end
    @version = validate_names(version, touched)
  end

  # ------------------------------------------------------------------
  # Publishing
  # ------------------------------------------------------------------

  # Validates and atomically publishes one batch of edits.  Every edit is
  # positioned against the pre-edit document:
  #
  #   hot.publish([
  #     { op: :insert,  before: 4, text: 'failover' },   # new line before old line 4
  #     { op: :replace, line: 4,   text: '[north] ! night shift' },
  #     { op: :delete,  line: 7 },
  #   ])
  #
  # While the candidate is validated, readers keep seeing the current
  # version; the switch happens as a single reference swap once the whole
  # batch is known to be readable.  Raises ConflictError (or ArgumentError
  # for malformed edits) and leaves the current version untouched
  # otherwise.  Returns self.
  def publish(edits)
    new_version = apply_edits(@version, edits)
    @lock.synchronize { @version = new_version }
    self
  end

  # Validates a batch like publish, but returns a new HotParseConfig
  # positioned at the candidate version instead of switching to it.  The
  # receiver keeps serving its current version.
  def preview(edits)
    spawn(apply_edits(@version, edits))
  end

  # ------------------------------------------------------------------
  # Lookups
  # ------------------------------------------------------------------

  # The value of a global parameter/flag, or a GroupView for a group,
  # or nil - mirroring ParseConfig#[] on the current document.
  def [](name)
    version = @version
    global = global_value(version, name)
    grouped = !version.headers_by_name.get(name).nil?
    if grouped
      # When both exist, the global write is necessarily the later one
      # (anything else would have been rejected), and wins - exactly like
      # ParseConfig's params hash.
      global ? global[0] : GroupView.new(version, name)
    else
      global && global[0]
    end
  end

  def key?(name)
    !self[name].nil?
  end

  # All parameter/group names, in the order a fresh ParseConfig read of
  # the current document would produce them.
  def get_params
    version = @version
    first_seen = {}
    version.headers_by_name.each do |name, keys|
      first_seen[name] = keys.min_key
    end
    version.global_params.each do |name, lines|
      key = lines.min_key
      first_seen[name] = key if !first_seen[name] || key < first_seen[name]
    end
    version.flags.each do |name, keys|
      key = keys.min_key
      first_seen[name] = key if !first_seen[name] || key < first_seen[name]
    end
    first_seen.sort_by { |_name, key| key }.map(&:first)
  end

  # Group names in first-appearance order, like ParseConfig#get_groups.
  def get_groups
    names = []
    @version.headers_by_name.each do |name, keys|
      names << [keys.min_key, name]
    end
    names.sort_by(&:first).map(&:last)
  end

  alias groups get_groups

  # A plain hash equivalent of the current document (materialized on
  # demand; lookups themselves never build this).
  def params
    result = {}
    get_params.each do |name|
      value = self[name]
      result[name] = value.is_a?(GroupView) ? value.to_h : value
    end
    result
  end

  alias to_h params

  # The physical line number (1-based, in the current document) of the
  # rule that produces the given value, or nil when there is no such
  # rule.  With one argument, locates a global parameter/flag (or the
  # first header of a group); with two, locates a parameter inside a
  # group.
  def line_of(name, key = nil)
    version = @version
    line_key =
      if key.nil?
        global = global_value(version, name)
        if global
          global[1]
        else
          keys = version.headers_by_name.get(name)
          keys && keys.min_key
        end
      else
        entry = HotParseConfig.find_in_group(version, name, key)
        entry && entry.line_key
      end
    line_key && version.lines.rank(line_key) + 1
  end

  def ==(other)
    if other.is_a?(HotParseConfig)
      to_h == other.to_h && get_groups == other.get_groups
    elsif defined?(ParseConfig) && other.is_a?(ParseConfig)
      params == other.params && get_groups == other.groups
    else
      false
    end
  end

  # Finds the winning entry for a parameter inside a group: sections of
  # the group are searched from the latest document position backwards,
  # so later sections override earlier ones (ParseConfig merge
  # semantics).  Used by HotParseConfig#[] and GroupView.
  def self.find_in_group(version, name, key)
    sections_of = version.groups_map.get(name)
    return nil unless sections_of

    found = nil
    sections_of.reverse_each do |section_id, _|
      section = version.sections.get(section_id)
      occurrences = section && section.by_name.get(key)
      if occurrences
        line_key = occurrences.max_key
        found = Entry.new(occurrences.get(line_key), line_key)
        break
      end
    end
    found
  end

  # Hash-like read-only view of one group of a document version.
  class GroupView
    include Enumerable

    def initialize(version, name)
      @version = version
      @name = name
    end

    def [](key)
      entry = HotParseConfig.find_in_group(@version, @name, key)
      entry && entry.value
    end

    def key?(key)
      !self[key].nil?
    end

    # Physical line number (1-based, in this document version) of the
    # rule that produces the given key, or nil.
    def line_of(key)
      entry = HotParseConfig.find_in_group(@version, @name, key)
      entry && @version.lines.rank(entry.line_key) + 1
    end

    def each(&block)
      return enum_for(:each) unless block

      to_h.each(&block)
    end

    def to_h
      result = {}
      sections_of = @version.groups_map.get(@name)
      return result unless sections_of

      sections_of.each do |section_id, _|
        section = @version.sections.get(section_id)
        section.by_line.each do |line_key, param_name|
          result[param_name] = section.by_name.get(param_name).get(line_key)
        end
      end
      result
    end

    def keys
      to_h.keys
    end

    def values
      to_h.values
    end

    def size
      to_h.size
    end

    def ==(other)
      if other.is_a?(GroupView)
        to_h == other.to_h
      elsif other.is_a?(Hash)
        to_h == other
      else
        false
      end
    end

    def inspect
      to_h.inspect
    end
  end

  private

  def spawn(version)
    copy = self.class.allocate
    copy.instance_variable_set(:@config_file, @config_file)
    copy.instance_variable_set(:@separator, @separator)
    copy.instance_variable_set(:@comments, @comments)
    copy.instance_variable_set(:@split_regex, @split_regex)
    copy.instance_variable_set(:@lock, Mutex.new)
    copy.instance_variable_set(:@version, version)
    copy
  end

  def empty_version
    t = EMPTY_TREAP
    Version.new(t, t, t, t, t, t, t, t, t)
  end

  # ------------------------------------------------------------------
  # Line classification (mirrors ParseConfig#import_config exactly)
  # ------------------------------------------------------------------

  def escaped_comment_regex
    /[#{Regexp.escape(@comments.join(''))}]/
  end

  # Returns [:comment] / [:blank] / [:param, name, value] /
  # [:header, name] / [:flag, name] for a stripped line.
  def classify(line)
    @comments.each do |comment|
      return [:comment] if /^#{comment}/.match(line)
    end

    if /#{@split_regex}/.match(line)
      param, value = line.split(/#{@split_regex}/, 2)
      name = param.to_s.chomp.strip
      value = value.chomp.strip
      new_value = ''
      if value
        if value =~ /^['"](.*)['"]$/
          new_value = Regexp.last_match(1)
        else
          new_value = value
        end
      end
      [:param, name, new_value]
    elsif (match = /^\[(.+)\](\s*#{escaped_comment_regex}+.*)?$/.match(line))
      [:header, match[1]]
    elsif /\w+/.match(line)
      [:flag, line.to_s.chomp.strip]
    else
      [:blank]
    end
  end

  # ------------------------------------------------------------------
  # Batch application
  # ------------------------------------------------------------------

  def apply_edits(version, edits)
    deletes, replaces, inserts = normalize_edits(version, edits)

    old_lines = version.lines
    lines = old_lines
    deletes.each_key { |key| lines = lines.delete(key) }
    replaces.each_key { |key| lines = lines.set(key, replaces[key]) }
    inserts.each { |key, text| lines = lines.set(key, text) }
    version = version.with(lines: lines)

    touched = {}

    # Remove plain rules before headers, so that header re-governing is
    # checked against the final state of the rules it affects.
    removals = (deletes.keys + replaces.keys).sort
    header_removals, other_removals = removals.partition do |key|
      classify(old_lines.get(key)).first == :header
    end
    (other_removals + header_removals).each do |key|
      version = remove_line(version, key, old_lines.get(key), touched)
    end

    additions = replaces.to_a + inserts
    additions.sort_by!(&:first)
    additions.each do |key, text|
      version = add_line(version, key, text, touched)
    end

    validate_names(version, touched)
  end

  def normalize_edits(version, edits)
    count = version.lines.size
    deletes = {}
    replaces = {}
    inserts = []
    insert_cursors = {}

    edits.each do |edit|
      case edit[:op]
      when :delete
        key = line_key_for(version, edit[:line], count)
        if deletes.key?(key) || replaces.key?(key)
          raise ArgumentError, "line #{edit[:line]} is edited more than once"
        end

        deletes[key] = true
      when :replace
        key = line_key_for(version, edit[:line], count)
        if deletes.key?(key) || replaces.key?(key)
          raise ArgumentError, "line #{edit[:line]} is edited more than once"
        end

        text = edit[:text]
        raise ArgumentError, 'a :replace edit requires :text' if text.nil?

        replaces[key] = text.to_s.strip
      when :insert
        before = edit[:before]
        unless before.is_a?(Integer) && before >= 1 && before <= count + 1
          raise ArgumentError, "insert position #{before.inspect} out of range 1..#{count + 1}"
        end

        text = edit[:text]
        raise ArgumentError, 'an :insert edit requires :text' if text.nil?

        low = insert_cursors[before]
        low ||= version.lines.select_key(before - 2) if before > 1
        high = before <= count ? version.lines.select_key(before - 1) : nil
        key = midpoint_key(low, high)
        insert_cursors[before] = key
        inserts << [key, text.to_s.strip]
      else
        raise ArgumentError, "unknown edit op: #{edit[:op].inspect}"
      end
    end

    [deletes, replaces, inserts]
  end

  def line_key_for(version, line, count)
    unless line.is_a?(Integer) && line >= 1 && line <= count
      raise ArgumentError, "line #{line.inspect} out of range 1..#{count}"
    end

    version.lines.select_key(line - 1)
  end

  # Line keys are exact rationals, so a fresh key always exists between
  # two neighbours (and before the first / after the last line).
  def midpoint_key(low, high)
    if low.nil? && high.nil?
      0
    elsif low.nil?
      high - 1
    elsif high.nil?
      low + 1
    else
      Rational(low + high, 2)
    end
  end

  # ------------------------------------------------------------------
  # Semantic index maintenance.  Every operation only touches the lines
  # and sections affected by an edit; unchanged sections (even ones with
  # a hundred thousand rules) are shared between versions.
  # ------------------------------------------------------------------

  def add_line(version, key, line, touched)
    type, name, value = classify(line)
    case type
    when :param
      add_param(version, key, name, value, touched)
    when :header
      add_header(version, key, name, touched)
    when :flag
      touched[name] = true
      add_flag(version, key, name)
    else
      version
    end
  end

  def remove_line(version, key, line, touched)
    type, name, = classify(line)
    case type
    when :param
      remove_param(version, key, name, touched)
    when :header
      remove_header(version, key, touched)
    when :flag
      touched[name] = true
      version.with(flags: nested_delete(version.flags, name, key))
    else
      version
    end
  end

  def add_param(version, key, name, value, touched)
    governor = version.headers.floor_key(key)
    if governor.nil?
      touched[name] = true
      version.with(global_params: nested_add(version.global_params, name, key, value))
    else
      governor_name = version.headers.get(governor)
      touched[governor_name] = true

      section_id = section_id_for(version, governor, key)
      section = version.sections.get(section_id)
      if section.nil?
        section = Section.new(EMPTY_TREAP, EMPTY_TREAP)
        version = version.with(
          governor: version.governor.set(section_id, governor),
          by_governor: nested_add(version.by_governor, governor, section_id),
          groups_map: nested_add(version.groups_map, governor_name, section_id)
        )
      end

      by_name = section.by_name
      occurrences = by_name.get(name) || EMPTY_TREAP
      by_name = by_name.set(name, occurrences.set(key, value))
      by_line = section.by_line.set(key, name)
      version.with(sections: version.sections.set(section_id, Section.new(by_name, by_line)))
    end
  end

  def remove_param(version, key, name, touched)
    governor = version.headers.floor_key(key)
    if governor.nil?
      touched[name] = true
      version.with(global_params: nested_delete(version.global_params, name, key))
    else
      touched[version.headers.get(governor)] = true
      section_id = section_id_for(version, governor, key)
      section = version.sections.get(section_id)
      return version unless section

      by_name = section.by_name
      occurrences = by_name.get(name)
      if occurrences
        occurrences = occurrences.delete(key)
        by_name = occurrences.empty? ? by_name.delete(name) : by_name.set(name, occurrences)
      end
      by_line = section.by_line.delete(key)

      if by_line.empty?
        governor_name = version.headers.get(governor)
        version.with(
          sections: version.sections.delete(section_id),
          governor: version.governor.delete(section_id),
          by_governor: nested_delete(version.by_governor, governor, section_id),
          groups_map: nested_delete(version.groups_map, governor_name, section_id)
        )
      else
        version.with(sections: version.sections.set(section_id, Section.new(by_name, by_line)))
      end
    end
  end

  def add_header(version, key, name, touched)
    touched[name] = true

    governor = version.headers.floor_key(key)
    touched[version.headers.get(governor)] = true if governor
    version = version.with(
      headers: version.headers.set(key, name),
      headers_by_name: nested_add(version.headers_by_name, name, key)
    )

    version =
      if governor.nil?
        # Rules after the new header (previously global) become its section.
        kept = EMPTY_TREAP
        moved_by_name = EMPTY_TREAP
        moved_by_line = EMPTY_TREAP
        version.global_params.each do |param_name, param_lines|
          kept_lines, moved_lines = param_lines.split(key)
          kept = kept.set(param_name, kept_lines) unless kept_lines.empty?
          next if moved_lines.empty?

          moved_by_name = moved_by_name.set(param_name, moved_lines)
          moved_lines.each do |line_key, _value|
            moved_by_line = moved_by_line.set(line_key, param_name)
          end
        end
        version = version.with(global_params: kept)
        if moved_by_line.empty?
          version
        else
          version.with(
            sections: version.sections.set(key, Section.new(moved_by_name, moved_by_line)),
            governor: version.governor.set(key, key),
            by_governor: nested_add(version.by_governor, key, key),
            groups_map: nested_add(version.groups_map, name, key)
          )
        end
      else
        section_id = section_id_for(version, governor, key)
        section = version.sections.get(section_id)
        if section.nil? || section.by_line.succ_key(key).nil?
          version
        else
          # The header lands inside an existing section: rules after it
          # move into a new section governed by the new header.  When
          # nothing precedes the header inside the section (the common
          # case of a renamed header), the section's indexes are reused
          # as-is instead of being rebuilt rule by rule.
          head_lines, tail_lines = section.by_line.split(key)
          tail_by_name =
            if head_lines.empty?
              section.by_name
            else
              rebuild_by_name(tail_lines, section.by_name)
            end

          if head_lines.empty?
            version = drop_section(version, section_id)
          else
            head_by_name = rebuild_by_name(head_lines, section.by_name)
            version = version.with(
              sections: version.sections.set(section_id, Section.new(head_by_name, head_lines))
            )
          end

          version.with(
            sections: version.sections.set(key, Section.new(tail_by_name, tail_lines)),
            governor: version.governor.set(key, key),
            by_governor: nested_add(version.by_governor, key, key),
            groups_map: nested_add(version.groups_map, name, key)
          )
        end
      end

    # Sections of the previous governor that start after the new header
    # are now governed by it.
    unless governor.nil?
      governed = version.by_governor.get(governor)
      if governed
        _earlier, later = governed.split(key)
        later.each do |section_id, _|
          governor_name = version.headers.get(governor)
          version = version.with(
            governor: version.governor.set(section_id, key),
            by_governor: nested_add(nested_delete(version.by_governor, governor, section_id), key, section_id),
            groups_map: nested_add(
              nested_delete(version.groups_map, governor_name, section_id),
              name, section_id
            )
          )
        end
      end
    end

    version
  end

  def remove_header(version, key, touched)
    name = version.headers.get(key)
    touched[name] = true
    version = version.with(
      headers: version.headers.delete(key),
      headers_by_name: nested_delete(version.headers_by_name, name, key)
    )

    governed = version.by_governor.get(key)
    return version unless governed

    version = version.with(by_governor: version.by_governor.delete(key))
    governor = version.headers.floor_key(key)
    governed.each do |section_id, _|
      section = version.sections.get(section_id)
      if governor.nil?
        # No preceding header left: the rules become global parameters.
        global_params = version.global_params
        section.by_line.each do |line_key, param_name|
          touched[param_name] = true
          global_params = nested_add(global_params, param_name, line_key,
                                     section.by_name.get(param_name).get(line_key))
        end

        version = version.with(
          global_params: global_params,
          sections: version.sections.delete(section_id),
          governor: version.governor.delete(section_id),
          groups_map: nested_delete(version.groups_map, name, section_id)
        )
      else
        governor_name = version.headers.get(governor)
        touched[governor_name] = true
        version = version.with(
          governor: version.governor.set(section_id, governor),
          by_governor: nested_add(version.by_governor, governor, section_id),
          groups_map: nested_add(
            nested_delete(version.groups_map, name, section_id),
            governor_name, section_id
          )
        )
      end
    end
    version
  end

  def add_flag(version, key, name)
    version.with(flags: nested_add(version.flags, name, key))
  end

  # Validates every touched name of the candidate document, mirroring
  # exactly the two ways a plain ParseConfig read can fail on a name:
  # a group header that meets a global parameter or flag of the same
  # name standing before it, and a group rule hidden behind a bare flag
  # of the group's name.  Checking the finished candidate (instead of
  # each edit in flight) keeps reordered and combined edits consistent
  # with a full re-read.
  def validate_names(version, touched)
    touched.each_key { |name| validate_name(version, name) }
    version
  end

  def validate_name(version, name)
    headers_of = version.headers_by_name.get(name)
    flag_lines = version.flags.get(name)
    param_lines = version.global_params.get(name)

    if headers_of
      first_header = headers_of.min_key

      # The global write standing when the first header of this name is
      # read, if any, collides with it.
      standing_key = nil
      standing_kind = nil
      if param_lines
        standing_key = param_lines.max_key
        standing_kind = 'global param'
      end
      if flag_lines && (flag_key = flag_lines.floor_key(first_header)) &&
         (standing_key.nil? || flag_key > standing_key)
        standing_key = flag_key
        standing_kind = 'global flag'
      end
      if standing_key
        conflict!(version, name, standing_kind, standing_key, 'group header', first_header)
      end

      # Any later header of the name meets the earliest flag before it.
      if flag_lines && (later_header = headers_of.succ_key(flag_lines.min_key))
        conflict!(version, name, 'global flag', flag_lines.floor_key(later_header),
                  'group header', later_header)
      end
    end

    return unless flag_lines

    # A bare flag hides every rule of the same-named group that follows
    # it (no header of the name can intervene, or the check above would
    # already have raised).
    sections_of = version.groups_map.get(name)
    return unless sections_of

    first_flag = flag_lines.min_key
    hidden = nil
    sections_of.each do |section_id, _|
      section = version.sections.get(section_id)
      next unless section

      if (param_key = section.by_line.succ_key(first_flag)) && (hidden.nil? || param_key < hidden)
        hidden = param_key
      end
    end
    return unless hidden

    conflict!(version, name, 'global flag', flag_lines.floor_key(hidden), 'group param', hidden)
  end

  def conflict!(version, name, first_kind, first_key, second_kind, second_key)
    first_line = version.lines.rank(first_key) + 1
    second_line = version.lines.rank(second_key) + 1
    raise ConflictError,
          "#{name}: #{first_kind} at line #{first_line} " \
          "conflicts with #{second_kind} at line #{second_line}"
  end

  # The id of the section that contains a rule at the given line key:
  # the section of its governor, or the latest section that split off
  # from it before the line.
  def section_id_for(version, governor, key)
    governed = version.by_governor.get(governor)
    section_id = governed && governed.floor_key(key)
    section_id || governor
  end

  def drop_section(version, section_id)
    governor = version.governor.get(section_id)
    governor_name = governor && version.headers.get(governor)
    version = version.with(
      sections: version.sections.delete(section_id),
      governor: version.governor.delete(section_id)
    )
    version = version.with(by_governor: nested_delete(version.by_governor, governor, section_id)) if governor
    if governor_name
      version = version.with(groups_map: nested_delete(version.groups_map, governor_name, section_id))
    end

    version
  end

  def rebuild_by_name(by_line, by_name)
    result = EMPTY_TREAP
    by_line.each do |line_key, param_name|
      result = nested_add(result, param_name, line_key, by_name.get(param_name).get(line_key))
    end

    result
  end

  def nested_add(map, name, key, value = true)
    inner = map.get(name) || EMPTY_TREAP
    map.set(name, inner.set(key, value))
  end

  def nested_delete(map, name, key)
    inner = map.get(name)
    return map unless inner

    inner = inner.delete(key)
    inner.empty? ? map.delete(name) : map.set(name, inner)
  end

  # The current global value for a name (bare flags anywhere plus
  # parameters before the first header; the latest write wins), as
  # [value, line_key], or nil.
  def global_value(version, name)
    flag_lines = version.flags.get(name)
    flag_key = flag_lines && flag_lines.max_key
    param_lines = version.global_params.get(name)
    param_key = param_lines && param_lines.max_key
    if flag_key && param_key
      flag_key > param_key ? [true, flag_key] : [param_lines.get(param_key), param_key]
    elsif flag_key
      [true, flag_key]
    elsif param_key
      [param_lines.get(param_key), param_key]
    end
  end
end
