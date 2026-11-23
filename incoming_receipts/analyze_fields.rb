# Visibility-aware purchase-field extraction for the incoming receipt batch.
#
# Scope declared for this batch (anything else is flagged to the field it
# lands in):
#   * horizontal fonts only
#   * rotation limited to multiples of 90 degrees (axis-aligned CTM)
#   * rectangular clipping paths
#   * opaque fills, no transparency blending
#
# Method:
#   * Glyph boxes come from HexaPDF's decoded + transformed per-glyph boxes
#     (HexaPDF::Content::Processor#decode_text_with_positioning); with the
#     declared scope every glyph maps to an axis-aligned rectangle on the page.
#   * The page is replayed in paint order, tracking the clipping region
#     (intersection of rectangles) across q/Q and across Form XObject
#     invocations. Each Form XObject invocation additionally intersects the
#     form's own BBox, per invocation - two invocations of the same form
#     object are evaluated independently.
#   * A glyph's effective on-page box is its rectangle intersected with the
#     clip in effect when it was painted. Later opaque fills (each limited to
#     its own clip) subtract from that area. A character is fully readable
#     only if its complete glyph box remains visible.
#   * Fields whose original characters are fully covered but that carry a
#     complete, fully visible replacement value on top are released with the
#     replacement value. Fields that cannot be read completely are reported
#     with the verifiable visible ranges of each character; the original text
#     object is never used to complete them.
#
# Usage:
#   ruby analyze_fields.rb [receipts.pdf] [fields.json]
# Writes field_report.json and field_report.md next to the manifest.

require 'json'
require 'hexapdf'
require 'hexapdf/content/parser'

EPS = 1e-6

# Region algebra on axis-aligned rectangles. A region is a list of
# [x0, y0, x1, y1] rectangles, not necessarily disjoint.
module Region
  module_function

  def empty?(rect)
    rect[2] - rect[0] < EPS || rect[3] - rect[1] < EPS
  end

  def intersect_rect(a, b)
    r = [[a[0], b[0]].max, [a[1], b[1]].max, [a[2], b[2]].min, [a[3], b[3]].min]
    empty?(r) ? nil : r
  end

  def intersect(region_a, region_b)
    region_a.flat_map {|ra| region_b.map {|rb| intersect_rect(ra, rb) } }.compact
  end

  def subtract_rect(rect, other)
    inter = intersect_rect(rect, other)
    return [rect] unless inter
    x0, y0, x1, y1 = rect
    ix0, iy0, ix1, iy1 = inter
    out = []
    out << [x0, y0, x1, iy0] if iy0 - y0 > EPS
    out << [x0, iy1, x1, y1] if y1 - iy1 > EPS
    out << [x0, iy0, ix0, iy1] if ix0 - x0 > EPS
    out << [ix1, iy0, x1, iy1] if x1 - ix1 > EPS
    out
  end

  def subtract(region, others)
    others.reduce(region) do |current, other|
      current.flat_map {|rect| subtract_rect(rect, other) }
    end
  end

  # Decomposes the region into disjoint rectangles via coordinate compression
  # and merges the cells back into maximal strips.
  def normalize(rects)
    rects = rects.reject {|r| empty?(r) }
    return [] if rects.empty?
    xs = rects.flat_map {|r| [r[0], r[2]] }.uniq.sort
    ys = rects.flat_map {|r| [r[1], r[3]] }.uniq.sort
    cells = []
    xs.each_cons(2) do |xa, xb|
      ys.each_cons(2) do |ya, yb|
        cx = (xa + xb) / 2.0
        cy = (ya + yb) / 2.0
        cells << [xa, ya, xb, yb] if rects.any? {|r| cx > r[0] && cx < r[2] && cy > r[1] && cy < r[3] }
      end
    end
    strips = []
    cells.group_by {|c| [c[1], c[3]] }.each do |(ya, yb), band|
      band.sort_by! {|c| c[0] }
      band.each do |c|
        last = strips.last
        if last && (last[1] - ya).abs < EPS && (last[3] - yb).abs < EPS && (last[2] - c[0]).abs < EPS
          last[2] = c[2]
        else
          strips << [c[0], ya, c[2], yb]
        end
      end
    end
    strips.sort_by! {|s| [s[0], s[2], s[1]] }
    merged = []
    strips.each do |s|
      last = merged.last
      if last && (last[0] - s[0]).abs < EPS && (last[2] - s[2]).abs < EPS && (last[3] - s[1]).abs < EPS
        last[3] = s[3]
      else
        merged << s.dup
      end
    end
    merged
  end

  def area(rects)
    normalize(rects).sum {|r| (r[2] - r[0]) * (r[3] - r[1]) }
  end
end

# Replays one page (including nested Form XObjects) in paint order and
# records glyph, occluder and flag events with full provenance.
class FieldProcessor < HexaPDF::Content::Processor
  def initialize(page_box)
    super()
    @clip = [page_box]
    @clip_stack = []
    @path = []
    @events = []
    @seq = 0
    @form_stack = []
    @stream_label = nil
    @pending_op = nil
    @pending_offset = nil
  end

  attr_reader :events

  # Walks a decoded content stream, tracking operator positions, and feeds
  # every operator to this processor.
  def walk_stream(data, label)
    previous_label = @stream_label
    @stream_label = label
    tokenizer = HexaPDF::Content::Tokenizer.new(data, raise_on_eos: true)
    parser = HexaPDF::Content::Parser.new
    params = []
    loop do
      obj = tokenizer.next_object(allow_keyword: true)
      if obj.kind_of?(HexaPDF::Tokenizer::Token)
        params = parser.send(:parse_inline_image, tokenizer) if obj == 'BI'
        @pending_op = obj.to_s
        @pending_offset = tokenizer.pos
        process(obj.to_sym, params)
        params = []
      else
        params << obj
      end
    end
  ensure
    @stream_label = previous_label
  end

  private

  def next_seq!
    @seq += 1
  end

  def flag(type, detail, region: nil)
    @events << {kind: :flag, seq: next_seq!, type: type, detail: detail,
                region: region || @clip.map(&:dup),
                stream: @stream_label, operator: @pending_op, offset: @pending_offset,
                forms: @form_stack.map(&:dup)}
  end

  def ctm_axis_aligned?
    ctm = graphics_state.ctm
    (ctm.b.abs < EPS && ctm.c.abs < EPS) || (ctm.a.abs < EPS && ctm.d.abs < EPS)
  end

  def check_ctm!(context)
    flag(:non_axis_aligned_matrix, "CTM not a 90-degree multiple after #{context}") unless ctm_axis_aligned?
  end

  def transform_point(x, y)
    graphics_state.ctm.evaluate(x, y)
  end

  def transform_rect(rect)
    points = [[rect[0], rect[1]], [rect[2], rect[1]],
              [rect[2], rect[3]], [rect[0], rect[3]]].map {|x, y| transform_point(x, y) }
    xs = points.map(&:first)
    ys = points.map(&:last)
    [xs.min, ys.min, xs.max, ys.max]
  end

  # Returns the page-space region of the current path; flags and falls back
  # to bounding boxes for non-rectangular subpaths.
  def path_region(kind)
    rects = []
    @path.each do |sub|
      case sub[:kind]
      when :rect
        rects << sub[:rect]
      else
        flag(:"non_rect_#{kind}", "non-rectangular subpath used for #{kind}; bounding box used")
        pts = sub[:pts]
        rects << [pts.map(&:first).min, pts.map(&:last).min,
                  pts.map(&:first).max, pts.map(&:last).max]
      end
    end
    rects
  end

  def color_description
    color = graphics_state.fill_color
    color ? color.to_s : 'unknown'
  rescue StandardError
    'unknown'
  end

  # -- graphics state -------------------------------------------------

  def save_graphics_state
    @clip_stack.push(@clip)
  end

  def restore_graphics_state
    @clip = @clip_stack.pop || @clip
  end

  def concatenate_matrix(_a, _b, _c, _d, _e, _f)
    check_ctm!('cm operator')
  end

  def set_graphics_state_parameters(name)
    gs = graphics_state
    problems = []
    problems << 'soft mask' if gs.soft_mask && gs.soft_mask != :None
    problems << "fill alpha #{gs.fill_alpha}" if gs.fill_alpha && gs.fill_alpha < 1 - EPS
    problems << "stroke alpha #{gs.stroke_alpha}" if gs.stroke_alpha && gs.stroke_alpha < 1 - EPS
    blend = gs.blend_mode
    problems << "blend mode #{blend.inspect}" if blend && blend != :Normal && blend != [:Normal]
    flag(:transparency, "ExtGState #{name}: #{problems.join(', ')}") unless problems.empty?
  end

  # -- path construction ----------------------------------------------

  def move_to(x, y)
    @path << {kind: :poly, pts: [transform_point(x, y)]}
  end

  def line_to(x, y)
    if @path.empty?
      move_to(x, y)
    else
      @path.last[:pts] << transform_point(x, y)
    end
  end

  def close_subpath
    return if @path.empty?
    @path.last[:pts] << @path.last[:pts].first
  end

  def curve_to(*coords)
    mark_curve(coords)
  end

  def curve_to_no_first_control_point(*coords)
    mark_curve(coords)
  end

  def curve_to_no_second_control_point(*coords)
    mark_curve(coords)
  end

  def mark_curve(coords)
    x, y = coords.last(2)
    if @path.empty?
      @path << {kind: :curve, pts: [transform_point(x, y)]}
    else
      @path.last[:kind] = :curve
      @path.last[:pts] << transform_point(x, y)
    end
  end

  def append_rectangle(x, y, w, h)
    rect = transform_rect([x, y, x + w, y + h])
    if ctm_axis_aligned?
      @path << {kind: :rect, rect: rect}
    else
      pts = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]].map {|px, py| transform_point(px, py) }
      @path << {kind: :poly, pts: pts}
    end
  end

  # -- path painting ----------------------------------------------------

  def clip_path_non_zero
    apply_clip
  end

  def clip_path_even_odd
    apply_clip
  end

  def apply_clip
    region = path_region(:clip)
    @clip = Region.intersect(@clip, region) unless region.empty?
    @clip = [] if region.empty? && @path.empty?
  end

  def end_path
    @path.clear
  end

  def fill_path_non_zero
    record_fill
  end

  def fill_path_even_odd
    record_fill
  end

  def fill_and_stroke_path_non_zero
    record_fill
  end

  def fill_and_stroke_path_even_odd
    record_fill
  end

  def close_fill_and_stroke_path_non_zero
    record_fill
  end

  def close_fill_and_stroke_path_even_odd
    record_fill
  end

  def record_fill
    region = path_region(:fill)
    painted = Region.intersect(region, @clip)
    if graphics_state.fill_alpha && graphics_state.fill_alpha < 1 - EPS
      flag(:transparency, "fill with alpha #{graphics_state.fill_alpha} is not opaque")
    elsif graphics_state.soft_mask && graphics_state.soft_mask != :None
      flag(:transparency, 'fill under soft mask is not opaque')
    elsif !painted.empty?
      @events << {kind: :fill, seq: next_seq!, region: Region.normalize(painted),
                  color: color_description, stream: @stream_label,
                  operator: @pending_op, offset: @pending_offset,
                  forms: @form_stack.map(&:dup)}
    end
    @path.clear
  end

  def stroke_path
    @path.clear
  end

  def close_and_stroke_path
    @path.clear
  end

  def paint_shading(name)
    flag(:shading, "shading #{name} painted")
  end

  def inline_image(_dict, _data)
    flag(:inline_image, 'inline image painted',
         region: [transform_rect([0, 0, 1, 1])])
  end

  # -- XObjects ---------------------------------------------------------

  def paint_xobject(name)
    xobject = resources.xobject(name)
    unless xobject
      flag(:missing_xobject, "XObject #{name} not found")
      return
    end
    case xobject[:Subtype]
    when :Form
      invoke_form(name, xobject)
    when :Image
      flag(:image_xobject, "image XObject #{name} painted",
           region: [transform_rect([0, 0, 1, 1])])
    else
      flag(:other_xobject, "XObject #{name} of subtype #{xobject[:Subtype]}")
    end
  end

  def invoke_form(name, xobject)
    outer_resources = resources
    invocation = {name: "/#{name}", oid: (xobject.oid if xobject.respond_to?(:oid)),
                  stream: @stream_label, operator: @pending_op, offset: @pending_offset}
    @clip_stack.push(@clip)
    @form_stack.push(invocation)
    graphics_state.save
    graphics_state.ctm.premultiply(*xobject[:Matrix]) if xobject.key?(:Matrix)
    check_ctm!("matrix of form /#{name}")
    @clip = Region.intersect(@clip, [transform_rect(xobject[:BBox].value)])
    self.resources = xobject[:Resources] || @original_resources
    label = "form /#{name} (obj #{invocation[:oid]})"
    walk_stream(xobject.contents, label)
  ensure
    graphics_state.restore
    @clip = @clip_stack.pop
    self.resources = outer_resources
    @form_stack.pop
  end

  # -- text ---------------------------------------------------------------

  def show_text(data)
    handle_text(data)
  end

  def show_text_with_positioning(data)
    handle_text(data)
  end

  def handle_text(_data)
    trm = graphics_state.text_rendering_mode
    if trm && !(trm == :fill)
      flag(:text_rendering_mode, "text rendering mode #{trm.respond_to?(:name) ? trm.name : trm}")
    end
    if graphics_state.fill_alpha && graphics_state.fill_alpha < 1 - EPS
      flag(:transparency, "text painted with fill alpha #{graphics_state.fill_alpha}")
    end
    begin
      boxes = decode_text_with_positioning(_data)
    rescue StandardError => e
      flag(:unsupported_text, "text cannot be handled (#{e.message})")
      return
    end
    boxes.each do |glyph|
      points = glyph.points.each_slice(2).to_a
      xs = points.map(&:first)
      ys = points.map(&:last)
      bbox = [xs.min, ys.min, xs.max, ys.max]
      unless axis_aligned_quad?(points)
        flag(:non_axis_aligned_text, "glyph quad of #{glyph.string.inspect} is not axis-aligned")
      end
      @events << {kind: :glyph, seq: next_seq!, char: glyph.string, bbox: bbox,
                  clip: @clip.map(&:dup), forms: @form_stack.map(&:dup),
                  stream: @stream_label, operator: @pending_op, offset: @pending_offset}
    end
  end

  def axis_aligned_quad?(points)
    xs = points.map(&:first)
    ys = points.map(&:last)
    xs.uniq.length <= 2 && ys.uniq.length <= 2
  end
end

# Computes per-glyph visibility for one page and evaluates the fields of the
# manifest against the visible text.
class PageAnalysis
  def initialize(events)
    @glyphs = events.select {|e| e[:kind] == :glyph }
    @fills = events.select {|e| e[:kind] == :fill }
    @flags = events.select {|e| e[:kind] == :flag }
    compute_visibility
  end

  attr_reader :glyphs, :fills, :flags

  def compute_visibility
    @glyphs.each do |glyph|
      clipped = Region.intersect([glyph[:bbox]], glyph[:clip])
      occluders = @fills.select {|f| f[:seq] > glyph[:seq] }.
        flat_map {|f| Region.intersect(f[:region], clipped) }
      glyph[:clipped_region] = Region.normalize(clipped)
      glyph[:occluded_region] = Region.normalize(occluders)
      glyph[:visible_region] = Region.normalize(Region.subtract(clipped, occluders))
      glyph[:area] = Region.area([glyph[:bbox]])
      glyph[:visible_area] = Region.area(glyph[:visible_region])
      ratio = glyph[:area] < EPS ? 0.0 : glyph[:visible_area] / glyph[:area]
      glyph[:visible_ratio] = ratio
      glyph[:state] = if ratio > 1 - 1e-6
                        :full
                      elsif ratio < 1e-9
                        :hidden
                      else
                        :partial
                      end
    end
  end

  def covering_fills(glyph)
    @fills.select do |f|
      f[:seq] > glyph[:seq] && !Region.intersect(f[:region], glyph[:clipped_region]).empty?
    end
  end

  def evaluate_field(field)
    rect = field['rect']
    glyphs = @glyphs.select do |g|
      cx = (g[:bbox][0] + g[:bbox][2]) / 2.0
      cy = (g[:bbox][1] + g[:bbox][3]) / 2.0
      cx >= rect[0] && cx <= rect[2] && cy >= rect[1] && cy <= rect[3]
    end
    glyphs = sort_reading_order(glyphs, field['direction'])
    flags = @flags.select do |f|
      f[:region].any? {|r| Region.intersect_rect(r, rect) }
    end

    result = {glyphs: glyphs, flags: flags}
    if glyphs.empty?
      result[:status] = 'empty'
      result[:value] = nil
      return result
    end

    if glyphs.all? {|g| g[:state] == :full }
      result[:status] = 'ok'
      result[:value] = glyphs.map {|g| g[:char] }.join
      return result
    end

    hidden = glyphs.select {|g| g[:state] == :hidden }
    cover_seqs = hidden.flat_map {|g| covering_fills(g).map {|f| f[:seq] } }
    unless cover_seqs.empty?
      last_cover = cover_seqs.max
      early = glyphs.select {|g| g[:seq] < last_cover }
      late = glyphs.select {|g| g[:seq] > last_cover }
      if !late.empty? && late.all? {|g| g[:state] == :full } &&
         early.all? {|g| g[:state] == :hidden }
        result[:status] = 'replaced'
        result[:value] = late.map {|g| g[:char] }.join
        result[:covered_value_note] = 'original characters fully covered; ' \
          'complete replacement value painted on top is fully visible'
        return result
      end
    end

    result[:status] = 'partial'
    result[:value] = nil
    result[:visible_text] = glyphs.map {|g| g[:state] == :full ? g[:char] : "\u{25AF}" }.join
    result
  end

  def sort_reading_order(glyphs, direction)
    case direction
    when 'rtl'
      glyphs.sort_by {|g| [-(g[:bbox][0] + g[:bbox][2]) / 2.0, (g[:bbox][1] + g[:bbox][3]) / 2.0] }
    else # ltr
      glyphs.sort_by {|g| [(g[:bbox][0] + g[:bbox][2]) / 2.0, -(g[:bbox][1] + g[:bbox][3]) / 2.0] }
    end
  end
end

# -- reporting helpers ----------------------------------------------------

def fmt(num)
  format('%.2f', num)
end

def fmt_rect(rect)
  "[#{rect.map {|v| fmt(v) }.join(', ')}]"
end

def glyph_provenance(glyph)
  {
    form_invocation: glyph[:forms].map do |inv|
      "Do #{inv[:name]} (obj #{inv[:oid]}) @ #{inv[:stream]} offset #{inv[:offset]}"
    end,
    text_instruction: "#{glyph[:operator]} @ #{glyph[:stream]} offset #{glyph[:offset]}",
  }
end

def glyph_report(glyph)
  {
    char: glyph[:char],
    state: glyph[:state].to_s,
    visible_ratio: glyph[:visible_ratio].round(4),
    glyph_box: glyph[:bbox].map {|v| v.round(2) },
    clipped_box: glyph[:clipped_region].map {|r| r.map {|v| v.round(2) } },
    visible_region: glyph[:visible_region].map {|r| r.map {|v| v.round(2) } },
  }.merge(glyph_provenance(glyph))
end

def flag_report(flag)
  {
    type: flag[:type].to_s,
    detail: flag[:detail],
    region: flag[:region].map {|r| r.map {|v| v.round(2) } },
    instruction: "#{flag[:operator]} @ #{flag[:stream]} offset #{flag[:offset]}",
    form_invocation: flag[:forms].map {|inv| "Do #{inv[:name]} (obj #{inv[:oid]})" },
  }
end

# -- main -------------------------------------------------------------------

pdf_path = ARGV[0] || File.join(__dir__, 'receipts.pdf')
manifest_path = ARGV[1] || File.join(__dir__, 'fields.json')
manifest = JSON.parse(File.read(manifest_path))

doc = HexaPDF::Document.open(pdf_path) # read-only: the original is never modified
analyses = []
page_flags = []

doc.pages.each_with_index do |page, index|
  media = page.box(:media).value
  processor = FieldProcessor.new([media[0].to_f, media[1].to_f, media[2].to_f, media[3].to_f])
  rotation = page[:Rotate] || 0
  if rotation != 0
    processor.events << {kind: :flag, seq: 0, type: :page_rotation,
                         detail: "page rotation #{rotation} not supported", region: [],
                         stream: "page #{index + 1}", operator: nil, offset: nil, forms: []}
  end
  processor.resources = page.resources
  processor.walk_stream(page.contents, "page #{index + 1} contents")
  analyses << PageAnalysis.new(processor.events)
end

field_reports = []
manifest['fields'].each do |field|
  analysis = analyses[field['page'] - 1]
  result = analysis.evaluate_field(field)
  review = !result[:flags].empty?
  report = {
    id: field['id'],
    page: field['page'],
    rect: field['rect'],
    direction: field['direction'],
    status: result[:status],
    value: result[:value],
    review: review,
  }
  report[:visible_text] = result[:visible_text] if result[:visible_text]
  report[:note] = result[:covered_value_note] if result[:covered_value_note]
  report[:glyphs] = result[:glyphs].map {|g| glyph_report(g) }
  report[:provenance] = {
    form_invocations: result[:glyphs].flat_map {|g| g[:forms] }.
      map {|inv| "Do #{inv[:name]} (obj #{inv[:oid]}) @ #{inv[:stream]} offset #{inv[:offset]}" }.uniq,
    text_instructions: result[:glyphs].
      map {|g| "#{g[:operator]} @ #{g[:stream]} offset #{g[:offset]}" }.uniq,
  }
  report[:flags] = result[:flags].map {|f| flag_report(f) }
  field_reports << report
end

assigned_flag_seqs = field_reports.flat_map {|r| r[:flags] }.map {|f| f[:instruction] }
unassigned = []
analyses.each_with_index do |analysis, i|
  analysis.flags.each do |f|
    instr = "#{f[:operator]} @ #{f[:stream]} offset #{f[:offset]}"
    unassigned << flag_report(f).merge(page: i + 1) unless assigned_flag_seqs.include?(instr)
  end
end

output = {
  document: File.basename(pdf_path),
  fields: field_reports,
  flags_outside_fields: unassigned,
}

json_path = File.join(__dir__, 'field_report.json')
File.write(json_path, JSON.pretty_generate(output))

# -- markdown report --------------------------------------------------------

lines = []
lines << "# 进货回单栏位可见性报告"
lines << ""
lines << "- 原件: `#{File.basename(pdf_path)}`（只读打开，未修改）"
lines << "- 栏位清单: `#{File.basename(manifest_path)}`（#{manifest['fields'].length} 个栏位）"
lines << "- 方法: HexaPDF 解码换算后的逐字盒 ∩ 实际裁剪区域 − 之后的不透明填充；逐次 Form 调用独立结算"
lines << ""

field_reports.each do |r|
  title = case r[:status]
          when 'ok' then "可入仓"
          when 'replaced' then "可入仓（替代值）"
          when 'partial' then "不可完整读取"
          when 'empty' then "栏位内无文字"
          end
  title += "；含未声明绘制能力，需复核" if r[:review]
  lines << "## #{r[:id]} — #{title}"
  lines << ""
  lines << "- 页码: #{r[:page]}；栏位矩形: #{fmt_rect(r[:rect])}；阅读方向: #{r[:direction]}"
  if r[:value]
    lines << "- 交付串: `#{r[:value]}`"
  elsif r[:visible_text]
    lines << "- 可见部分: `#{r[:visible_text]}`（□ 表示无法核实的字，未从原文字对象补全）"
  else
    lines << "- 交付串: 无"
  end
  lines << "- 备注: #{r[:note]}" if r[:note]
  prov = r[:provenance]
  lines << "- 表格调用: #{prov[:form_invocations].empty? ? '页面直接内容' : prov[:form_invocations].join(' → ')}"
  lines << "- 文字指令: #{prov[:text_instructions].join('；')}"
  if r[:status] == 'partial'
    lines << "- 逐字可见范围:"
    r[:glyphs].each do |g|
      region = g[:visible_region].empty? ? '（无可见部分）' : g[:visible_region].map {|rr| fmt_rect(rr) }.join(' ∪ ')
      lines << "  - `#{g[:char]}` #{g[:state]} #{format('%.1f', g[:visible_ratio] * 100)}% 可见: #{region}"
    end
  end
  r[:flags].each do |f|
    lines << "- 未声明能力: #{f[:type]} — #{f[:detail]}（#{f[:instruction]}）"
  end
  lines << ""
end

unless unassigned.empty?
  lines << "## 栏位外的未声明能力"
  lines << ""
  unassigned.each do |f|
    lines << "- 第 #{f[:page]} 页: #{f[:type]} — #{f[:detail]}（#{f[:instruction]}）"
  end
  lines << ""
end

md_path = File.join(__dir__, 'field_report.md')
File.write(md_path, lines.join("\n"))
puts "written #{json_path}"
puts "written #{md_path}"
