# frozen_string_literal: true

require 'zip'
require 'stringio'

# Byte-range-strict display-name / comment rewriting for ZIP archives.
#
# Why this does not simply call `Zip::File#commit`:
#
#   * `commit` recompresses or re-copies entries through the writer, which
#     reorders and renormalises extra fields. On an archive whose headers are
#     not in Rubyzip's canonical order that moves bytes outside the ranges the
#     receiver authorised.
#   * The task fixes absolute offsets for the compressed payloads and for the
#     central-directory boundary. Any writer that recomputes sizes is therefore
#     not usable for the patch itself.
#
# So Rubyzip is used to parse the archive and, in verify.rb, to re-read the
# result through both public entry points. The patch is described as
# length-neutral splices over the original bytes.
#
# Invariants enforced for every edit:
#
#   1. Payloads are copied verbatim. CRC, compressed size and uncompressed size
#      are never recomputed.
#   2. Every structural offset is unchanged: directory start, each entry's
#      local-header offset, and every later record's position.
#   3. A name or comment may only grow if the same number of bytes is freed in
#      the same header from padding the receiver authorised removing. Funding
#      bytes never cross headers (local pays for local, central for central),
#      because the two extra fields are separate byte runs.
#   4. A local header is never moved. A gap between entries would break
#      `Zip::InputStream`, which seeks to `local_header_offset + header_size +
#      compressed_size` to find the next entry. This is why the prompt's "move
#      the local header earlier" option cannot satisfy both readers.
#   5. If a local header offset is referenced by more than one central record,
#      editing the name is still possible when the local header itself does not
#      move; a change that would move the header is refused, naming every
#      referencing record.
module ZipRelabel
  LOCAL_SIG         = [::Zip::LOCAL_ENTRY_SIGNATURE].pack('V')
  CDIR_SIG          = [::Zip::CENTRAL_DIRECTORY_ENTRY_SIGNATURE].pack('V')
  EOCD_SIG          = [::Zip::CentralDirectory::END_OF_CD_SIG].pack('V')
  ZIP64_EOCD_SIG    = [::Zip::CentralDirectory::ZIP64_END_OF_CD_SIG].pack('V')
  ZIP64_LOCATOR_SIG = [::Zip::CentralDirectory::ZIP64_EOCD_LOCATOR_SIG].pack('V')
  UINT32_MAX        = 0xFFFFFFFF

  class Error < StandardError; end

  # Raised when the requested edition cannot be expressed inside the allowed
  # ranges. `reasons` explains where the space went.
  class Refused < Error
    attr_reader :reasons

    def initialize(reasons)
      @reasons = Array(reasons)
      super(@reasons.join("\n"))
    end
  end

  LocalHeader = Struct.new(
    :offset, :gp_flags, :compression_method, :crc,
    :compressed_size, :size, :name_length, :extra_length,
    :name, :extra, :data_offset,
    keyword_init: true
  ) do
    def header_size
      30 + name_length + extra_length
    end
  end

  CentralRecord = Struct.new(
    :offset, :gp_flags, :compression_method, :crc,
    :compressed_size, :size, :name_length, :extra_length, :comment_length,
    :local_header_offset, :name, :extra, :comment,
    keyword_init: true
  ) do
    def header_size
      46 + name_length + extra_length + comment_length
    end

    def end_offset
      offset + header_size
    end

    def zip64_offset?
      local_header_offset == UINT32_MAX
    end
  end

  ExtraRecord = Struct.new(:id, :offset, :length, keyword_init: true) do
    def content_length
      length - 4
    end
  end

  # Funding spec: an extra-field id plus the maximum number of content bytes
  # that may be removed from it. Content, not whole records, is what gets
  # trimmed, so any byte count (including 3) can be matched exactly.
  Fund = Struct.new(:id, :max_bytes, keyword_init: true)

  # name:        replacement name, or nil to keep it
  # comment:     replacement central-directory comment, or nil to keep it
  # encoding:    :keep (default), :cp437 or :utf8; the latter two also set or
  #              clear general-purpose bit 11 in both headers
  # fund_local:  array of Fund that may be trimmed in this entry's local extra
  # fund_central: array of Fund for this entry's central extra
  # expect_offset: assert the local header starts here
  Edit = Struct.new(
    :entry, :name, :comment, :encoding, :fund_local, :fund_central, :expect_offset,
    keyword_init: true
  )

  module_function

  # ---------------------------------------------------------------- parsing

  def parse_local(bytes, offset)
    raise Error, "no local header at #{offset}" unless bytes[offset, 4] == LOCAL_SIG

    _version, gp_flags, method, _time, _date, crc, csize, usize, nlen, elen =
      bytes[offset + 4, 26].unpack('vvvvvVVVvv')
    body = offset + 30
    LocalHeader.new(
      offset: offset, gp_flags: gp_flags, compression_method: method, crc: crc,
      compressed_size: csize, size: usize, name_length: nlen, extra_length: elen,
      name: bytes[body, nlen].b, extra: bytes[body + nlen, elen].b,
      data_offset: body + nlen + elen
    )
  end

  def parse_central(bytes, offset)
    raise Error, "no central record at #{offset}" unless bytes[offset, 4] == CDIR_SIG

    f = bytes[offset + 4, 42].unpack('vvvvvvVVVvvvvvVV')
    body = offset + 46
    nlen, elen, clen = f[9], f[10], f[11]
    CentralRecord.new(
      offset: offset, gp_flags: f[2], compression_method: f[3], crc: f[6],
      compressed_size: f[7], size: f[8], name_length: nlen, extra_length: elen,
      comment_length: clen, local_header_offset: f[15],
      name: bytes[body, nlen].b, extra: bytes[body + nlen, elen].b,
      comment: bytes[body + nlen + elen, clen].b
    )
  end

  def directory_bounds(bytes)
    eocd = bytes.rindex(EOCD_SIG)
    raise Error, 'end of central directory signature not found' unless eocd

    _, _, _, entries, total, _size, cdir_offset, _clen = bytes[eocd, 22].unpack('VvvvvVVv')
    if entries == 0xFFFF || total == 0xFFFF || cdir_offset == UINT32_MAX
      locator = bytes.rindex(ZIP64_LOCATOR_SIG, eocd)
      raise Error, 'Zip64 end of central directory locator missing' unless locator

      zip64 = bytes[locator + 8, 8].unpack1('Q<')
      raise Error, 'Zip64 end of central directory record missing' unless bytes[zip64, 4] == ZIP64_EOCD_SIG

      total = bytes[zip64 + 32, 8].unpack1('Q<')
      cdir_offset = bytes[zip64 + 48, 8].unpack1('Q<')
    end
    [total, cdir_offset, eocd]
  end

  def central_records(bytes)
    total, cursor = directory_bounds(bytes)
    records = []
    total.times do
      record = parse_central(bytes, cursor)
      records << record
      cursor = record.end_offset
    end
    records
  end

  # Summarise an archive: every entry, the byte spans that a relabel would
  # touch, and every structural offset that must not move. This is the shape
  # of report to hand back when a requested edition cannot be expressed.
  def survey(bytes)
    records = central_records(bytes)
    offsets = records.group_by(&:local_header_offset)
    total, cdir, eocd = directory_bounds(bytes)
    {
      size: bytes.bytesize,
      directory_offset: cdir,
      eocd_offset: eocd,
      entries: records.map do |record|
        local = parse_local(bytes, record.local_header_offset)
        {
          name:            record.name,
          local_offset:    record.local_header_offset,
          local_header:    [local.offset, local.header_size],
          central_record:  [record.offset, record.header_size],
          data:            [local.data_offset, local.compressed_size],
          crc:             record.crc,
          name_bytes:      record.name_length,
          comment_bytes:   record.comment_length,
          local_extra:     extra_records(local.extra).map { |r| [r.id.unpack1('H*'), r.length] },
          central_extra:   extra_records(record.extra).map { |r| [r.id.unpack1('H*'), r.length] },
          zip64_offset:    record.zip64_offset?,
          shared_with:     (offsets[record.local_header_offset] || []).size - 1
        }
      end
    }
  end

  def extra_records(extra)
    extra = extra.b
    records = []
    i = 0
    while i + 4 <= extra.bytesize
      id = extra[i, 2].b
      len = extra[i + 2, 2].unpack1('v')
      break if i + 4 + len > extra.bytesize

      records << ExtraRecord.new(id: id, offset: i, length: len + 4)
      i += len + 4
    end
    records
  end

  # ------------------------------------------------------------------ names

  def encode_name(name, encoding)
    return [name.b, nil] if encoding.nil? || encoding == :keep

    case encoding
    when :cp437 then [name.encode('IBM437').b, false]
    when :utf8  then [name.encode('UTF-8').b, true]
    else raise Error, "unsupported name encoding #{encoding.inspect}"
    end
  end

  # --------------------------------------------------------------- funding

  # Remove exactly `wanted` bytes of content across the authorised extra-field
  # records. Returns [new_extra_bytes, removed]. Raises Refused when the
  # authorised fields cannot supply that exact amount.
  def free_exactly(extra, funds, wanted, side, entry)
    return [extra.b, 0] if wanted.zero?
    raise Refused, ["#{entry}: #{side} would need to grow (negative funding)"] if wanted.negative?

    funds = Array(funds).map { |f| f.is_a?(Fund) ? f : Fund.new(id: f, max_bytes: nil) }
    records = extra_records(extra)
    remaining = wanted
    edits = [] # [offset, content_length, new_content_length]
    detail = []

    funds.each do |fund|
      candidates = records.select { |r| r.id == fund.id.b }
      candidates.each do |record|
        break if remaining.zero?

        available = record.content_length
        available = [available, fund.max_bytes].min if fund.max_bytes
        take = [available, remaining].min
        next if take.zero?

        edits << [record.offset, record.content_length, record.content_length - take]
        detail << "#{fund.id.inspect}:#{take}"
        remaining -= take
      end
    end

    unless remaining.zero?
      supplied = wanted - remaining
      raise Refused,
            ["#{entry}: #{side} needs #{wanted} bytes but the authorised padding can supply " \
             "only #{supplied} (#{detail.empty? ? 'no matching extra field' : detail.join(', ')})"]
    end

    out = extra.b
    # Apply from the end so earlier offsets stay valid.
    edits.sort_by { |offset, _, _| -offset }.each do |offset, old_content, new_content|
      out[offset + 2, 2] = [new_content].pack('v')
      out.slice!(offset + 4 + new_content, old_content - new_content)
    end
    raise Error, "#{side}: funding changed extra length by " \
                 "#{extra.bytesize - out.bytesize} instead of #{wanted}" unless
      extra.bytesize - out.bytesize == wanted

    [out, wanted]
  end

  # -------------------------------------------------------------- planning

  Plan = Struct.new(:splices, :notes, :refusals, keyword_init: true)

  def plan(source, edits)
    source = source.dup.force_encoding('BINARY')
    records = central_records(source)
    by_name = records.group_by(&:name)
    by_offset = records.group_by(&:local_header_offset)

    splices = []
    notes = []
    refusals = []

    Array(edits).each do |edit|
      candidates = by_name[edit.entry.b] || []
      if candidates.empty?
        refusals << "#{edit.entry.inspect}: not found in the central directory"
        next
      end
      if candidates.size > 1
        shared = candidates.map(&:local_header_offset).uniq
        refusals << if shared.size == 1
                      "#{edit.entry.inspect}: name is used by #{candidates.size} directory " \
                      "records that share one local header at #{shared.first}; renaming the " \
                      'local header would also invalidate every other record pointing at it'
                    else
                      "#{edit.entry.inspect}: name is used by #{candidates.size} distinct " \
                      'records; disambiguate by offset before editing'
                    end
        next
      end
      record = candidates.first
      local = parse_local(source, record.local_header_offset)
      if local.name != record.name
        refusals << "#{edit.entry.inspect}: local name #{local.name.inspect} differs from " \
                    "central name #{record.name.inspect}"
        next
      end
      if edit.expect_offset && edit.expect_offset != record.local_header_offset
        refusals << "#{edit.entry.inspect}: expected local header at #{edit.expect_offset}, " \
                    "found #{record.local_header_offset}"
        next
      end

      name_bytes, efs = edit.name ? encode_name(edit.name, edit.encoding) : [record.name, nil]
      comment_bytes = edit.comment ? edit.comment.b : record.comment

      local_splice = local_note = central_splice = central_note = nil
      local_error = central_error = nil
      begin
        local_splice, local_note = plan_local(source, local, record, name_bytes, efs, edit)
      rescue Refused => e
        local_error = e.reasons
      end
      begin
        central_splice, central_note = plan_central(source, record, name_bytes, comment_bytes,
                                                    efs, edit)
      rescue Refused => e
        central_error = e.reasons
      end

      if local_error || central_error
        refusals.concat(Array(local_error), Array(central_error))
        next
      end

      splices << local_splice if local_splice
      splices << central_splice if central_splice
      notes << local_note if local_note
      notes << central_note if central_note
      notes << "#{edit.entry.inspect}: #{record.name.inspect} -> #{name_bytes.inspect}; " \
               "comment #{record.comment.inspect} -> #{comment_bytes.inspect}"
    end

    if by_offset.any? { |_offset, refs| refs.size > 1 }
      shared = by_offset.select { |_offset, refs| refs.size > 1 }
      shared.each do |offset, refs|
        names = refs.map { |r| r.name.inspect }
        notes << "local header at #{offset} is shared by #{refs.size} directory records " \
                 "(#{names.join(', ')}); it must not move"
      end
    end

    Plan.new(splices: splices, notes: notes, refusals: refusals)
  end

  def plan_local(source, local, record, name_bytes, efs, edit)
    gain = name_bytes.bytesize - local.name_length
    return [nil, nil] if gain.zero? && efs.nil? && name_bytes == local.name

    new_extra, removed = free_exactly(local.extra, edit.fund_local, [gain, 0].max,
                                      'local header', edit.entry)

    static = source[local.offset, 30].dup
    static[6, 2] = [patch_flags(static[6, 2].unpack1('v'), efs)].pack('v') unless efs.nil?
    static[26, 2] = [name_bytes.bytesize].pack('v')
    static[28, 2] = [new_extra.bytesize].pack('v')
    bytes = static + name_bytes + new_extra
    unless bytes.bytesize == local.header_size
      raise Error, "#{edit.entry}: local header length drift " \
                   "#{bytes.bytesize - local.header_size}"
    end

    [[local.offset, local.header_size, bytes],
     "#{edit.entry}: local name grew #{gain}, trimmed #{removed} bytes of local padding; " \
     "header stays at #{local.offset}"]
  end

  def plan_central(source, record, name_bytes, comment_bytes, efs, edit)
    gain = (name_bytes.bytesize - record.name_length) +
           (comment_bytes.bytesize - record.comment_length)
    new_extra, removed = free_exactly(record.extra, edit.fund_central, [gain, 0].max,
                                      'central record', edit.entry)

    static = source[record.offset, 46].dup
    static[8, 2] = [patch_flags(static[8, 2].unpack1('v'), efs)].pack('v') unless efs.nil?
    static[28, 2] = [name_bytes.bytesize].pack('v')
    static[30, 2] = [new_extra.bytesize].pack('v')
    static[32, 2] = [comment_bytes.bytesize].pack('v')
    bytes = static + name_bytes + new_extra + comment_bytes
    unless bytes.bytesize == record.header_size
      raise Error, "#{edit.entry}: central record length drift " \
                   "#{bytes.bytesize - record.header_size}"
    end

    [[record.offset, record.header_size, bytes],
     "#{edit.entry}: central record trimmed #{removed} bytes of padding; " \
     "local-header offset stays #{record.local_header_offset}"]
  end

  def patch_flags(flags, efs)
    return flags if efs.nil?

    efs ? flags | 0x0800 : flags & ~0x0800
  end

  # ----------------------------------------------------------------- apply

  # Walk the archive the way `Zip::InputStream` does: from offset 0, read a
  # local header, then jump to `offset + header_size + compressed_size`.
  # Every hop must land on the next declared local header, ending at the
  # directory. Returns a list of problems (empty means the chain is sound).
  #
  # This catches a subtle class of bug: trimming bytes out of a *structured*
  # extra field can produce a header that is valid on its own but that Rubyzip
  # re-expands when reading, so the computed next offset drifts by the number
  # of bytes removed. Padding in an unknown field round-trips exactly; a
  # partially drained known field may not.
  def stream_chain_problems(bytes)
    expected = central_records(bytes).map(&:local_header_offset).sort
    _total, directory, = directory_bounds(bytes)
    problems = []
    io = StringIO.new(bytes)
    io.binmode
    seen = []
    guard = 0
    while io.pos < directory
      guard += 1
      raise Error, 'stream walk did not terminate' if guard > expected.size + 2

      offset = io.pos
      entry = ::Zip::Entry.read_local_entry(io)
      if entry.nil?
        problems << "stream walk stopped at #{offset}; no local header there " \
                    "(declared: #{expected.inspect})"
        break
      end
      seen << offset
      next_at = offset + entry.calculate_local_header_size + entry.compressed_size
      break if next_at >= directory

      io.seek(next_at, IO::SEEK_SET)
    end
    unless seen == expected
      problems << "stream walk visited #{seen.inspect}; the central directory declares " \
                  "#{expected.inspect} (readers would disagree)"
    end
    problems
  end

  def apply(source, edits, allowed:)
    source = source.dup.force_encoding('BINARY')
    plan = plan(source, edits)
    raise Refused, plan.refusals unless plan.refusals.empty?

    ranges = Array(allowed).map { |r| r.is_a?(Range) ? (r.begin...r.end) : (r[0]...r[1]) }
    plan.splices.each do |offset, length, bytes|
      unless bytes.bytesize == length
        raise Error, "internal: splice at #{offset} is not length-neutral"
      end

      covered = ranges.any? { |r| r.begin <= offset && r.end >= offset + length }
      next if covered

      raise Refused, ["bytes #{offset}...#{offset + length} are outside every allowed range; " \
                      'nothing was written']
    end

    out = +''.b
    cursor = 0
    plan.splices.sort_by(&:first).each do |offset, length, bytes|
      raise Error, "overlapping splices at #{offset}" if offset < cursor

      out << source[cursor...offset] << bytes
      cursor = offset + length
    end
    out << source[cursor..]
    raise Error, 'output length changed' unless out.bytesize == source.bytesize

    # Post-condition: no structural offset moved, so no central record had to
    # change the width it uses for its local-header offset (4-byte field vs.
    # the 8-byte Zip64 value). This is why the Zip64-width trap cannot bite
    # here: the offset is never rewritten at all. Compare by position, because
    # names are expected to change.
    before = central_records(source).map(&:local_header_offset)
    after = central_records(out).map(&:local_header_offset)
    unless before == after
      raise Error, "a local-header offset changed:\n  before #{before.inspect}\n  after  #{after.inspect}"
    end

    chain = stream_chain_problems(out)
    unless chain.empty?
      raise Refused, chain.map { |p| "streaming reader would break: #{p}" }
    end

    [out, plan]
  end
end
