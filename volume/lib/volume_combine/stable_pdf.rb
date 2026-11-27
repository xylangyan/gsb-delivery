# frozen_string_literal: true

require 'combine_pdf'

module VolumeCombine
  # CombinePDF renames every name tree entry (CombinePDF_0000001, ...) and
  # rewrites matching destinations while parsing. The assembly needs the
  # original spellings and explicit control over the final name tree, so the
  # automatic rename is disabled here. The tool builds /Names, outlines and
  # all destinations itself instead of relying on the default merge behavior.
  class StablePDF < CombinePDF::PDF
    protected

    def rebuild_names(*)
      # Keep whatever name data was assigned; the combiner owns the final
      # /Names dictionary and replaces it before rendering.
      @names ||= {}
    end
  end
end
