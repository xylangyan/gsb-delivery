# frozen_string_literal: true

require 'roo/excelx/extractor'

module Roo
  class Excelx
    # Public: Parses an xl/drawings/drawingN.xml file. Each anchor in the
    #         drawing describes one placement of a picture on the worksheet:
    #         the same media file can be placed several times, and every
    #         placement keeps its own anchor coordinates and offsets.
    class Drawing < Excelx::Extractor
      # Internal: value object for a single xdr anchor.
      #
      # type     - :two_cell, :one_cell or :absolute
      # from/to  - Hash { row:, col:, row_offset:, col_offset: } zero based,
      #            exactly as stored in the anchor markers (nil when the
      #            anchor does not define the marker)
      # pos      - Hash { x:, y: } in EMU for absolute anchors
      # ext      - Hash { cx:, cy: } in EMU for oneCellAnchor/absoluteAnchor
      # embed    - relationship id of the embedded media (a:blip r:embed)
      # link     - relationship id of the linked media (a:blip r:link)
      # src_rect - Hash of the a:srcRect attributes when the picture is
      #            explicitly cropped, nil otherwise
      # name     - picture name from xdr:cNvPr
      Anchor = Struct.new(:type, :from, :to, :pos, :ext, :embed, :link, :src_rect, :name, keyword_init: true)

      def anchors
        @anchors ||= extract_anchors
      end

      private

      def extract_anchors
        return [] unless doc_exists?

        doc.xpath('/wsDr/*[self::twoCellAnchor or self::oneCellAnchor or self::absoluteAnchor]').map do |anchor_xml|
          pic = anchor_xml.xpath('./pic').first
          blip = pic&.xpath('./blipFill/blip')&.first
          src_rect = pic&.xpath('./blipFill/srcRect')&.first

          Anchor.new(
            type: anchor_type(anchor_xml.name),
            from: parse_marker(anchor_xml.xpath('./from').first),
            to: parse_marker(anchor_xml.xpath('./to').first),
            pos: parse_pos(anchor_xml.xpath('./pos').first),
            ext: parse_ext(anchor_xml.xpath('./ext').first),
            embed: blip && blip['embed'],
            link: blip && blip['link'],
            src_rect: src_rect && src_rect.attributes.transform_values(&:value),
            name: pic&.xpath('./nvPicPr/cNvPr')&.first&.[]('name')
          )
        end
      end

      def anchor_type(element_name)
        case element_name
        when 'twoCellAnchor' then :two_cell
        when 'oneCellAnchor' then :one_cell
        else :absolute
        end
      end

      def parse_marker(marker_xml)
        return unless marker_xml

        {
          row: marker_xml.xpath('./row').text.to_i,
          col: marker_xml.xpath('./col').text.to_i,
          row_offset: marker_xml.xpath('./rowOff').text.to_i,
          col_offset: marker_xml.xpath('./colOff').text.to_i
        }
      end

      def parse_pos(pos_xml)
        return unless pos_xml

        { x: pos_xml['x'].to_i, y: pos_xml['y'].to_i }
      end

      def parse_ext(ext_xml)
        return unless ext_xml

        { cx: ext_xml['cx'].to_i, cy: ext_xml['cy'].to_i }
      end
    end
  end
end
