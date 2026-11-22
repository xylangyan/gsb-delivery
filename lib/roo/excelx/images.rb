require 'roo/excelx/extractor'

module Roo
  class Excelx
    class Images < Excelx::Extractor

      # Returns: Hash { id1: extracted_file_name1 },
      # Example: { "rId1"=>"roo_media_image1.png",
      #            "rId2"=>"roo_media_image2.png",
      #            "rId3"=>"roo_media_image3.png" }
      def list
        @images ||= extract_images_names
      end

      # Returns: Hash { id1: { name:, target:, external: } }
      # Example: { "rId1"=>{ name: "roo_media_image1.png",
      #                      target: "../media/image1.png",
      #                      external: false },
      #            "rId2"=>{ name: nil,
      #                      target: "https://example.com/image2.png",
      #                      external: true } }
      # External relationships are declarations only: they have no
      # extractable media, so their name is nil and their target is the
      # linked location.
      def entries
        @entries ||= extract_image_entries
      end

      private

      def extract_images_names
        return {} unless doc_exists?

        doc.xpath('/Relationships/Relationship').each_with_object({}) do |rel, hash|
          hash[rel['Id']] = "roo" + rel['Target'].gsub(/\.\.\/|\//, '_')
        end
      end

      def extract_image_entries
        return {} unless doc_exists?

        doc.xpath('/Relationships/Relationship').each_with_object({}) do |rel, hash|
          external = rel['TargetMode'] == 'External'
          hash[rel['Id']] = {
            name: external ? nil : "roo" + rel['Target'].gsub(/\.\.\/|\//, '_'),
            target: rel['Target'],
            external: external
          }
        end
      end
    end
  end
end
