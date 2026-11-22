module Roo
  # A base error class for Roo. Most errors thrown by Roo should inherit from
  # this class.
  class Error < StandardError; end

  # Raised when Roo cannot find a header row that matches the given column
  # name(s).
  class HeaderRowNotFoundError < Error; end

  class FileNotFound < Error; end

  # Raised when a selected-sheet export cannot be completed without borrowing
  # content from another sheet, another workbook, or an unauthorized part.
  class ExportError < Error; end

  # Raised when a copy is internally inconsistent, e.g. a cell points at a
  # shared string that the copy does not contain.
  class InconsistentCopyError < ExportError; end
end
