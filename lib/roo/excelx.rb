require 'nokogiri'
require 'zip/filesystem'
require 'roo/link'
require 'roo/tempdir'
require 'roo/utils'
require 'forwardable'
require 'set'

module Roo
  class Excelx < Roo::Base
    extend Roo::Tempdir
    extend Forwardable

    ERROR_VALUES = %w(#N/A #REF! #NAME? #DIV/0! #NULL! #VALUE! #NUM!).to_set

    require 'roo/excelx/shared'
    require 'roo/excelx/workbook'
    require 'roo/excelx/package'
    require 'roo/excelx/shared_strings'
    require 'roo/excelx/styles'
    require 'roo/excelx/cell'
    require 'roo/excelx/sheet'
    require 'roo/excelx/relationships'
    require 'roo/excelx/comments'
    require 'roo/excelx/sheet_doc'
    require 'roo/excelx/coordinate'
    require 'roo/excelx/format'
    require 'roo/excelx/images'
    require 'roo/excelx/exporter'
    require 'roo/excelx/writer'
    require 'roo/excelx/auditor'

    delegate [:styles, :workbook, :shared_strings, :rels_files, :sheet_files, :comments_files, :image_rels, :image_files] => :@shared
    ExceedsMaxError = Class.new(StandardError)

    attr_reader :source_path

    # initialization and opening of a spreadsheet file
    # values for packed: :zip
    # optional cell_max (int) parameter for early aborting attempts to parse
    # enormous documents.
    def initialize(filename_or_stream, options = {})
      packed = options[:packed]
      file_warning = options.fetch(:file_warning, :error)
      cell_max = options.delete(:cell_max)
      sheet_options = {}
      sheet_options[:expand_merged_ranges] = (options[:expand_merged_ranges] || false)
      sheet_options[:no_hyperlinks] = (options[:no_hyperlinks] || false)
      sheet_options[:empty_cell] = (options[:empty_cell] || false)
      shared_options = {}

      shared_options[:disable_html_wrapper] = (options[:disable_html_wrapper] || false)
      unless is_stream?(filename_or_stream)
        file_type_check(filename_or_stream, %w[.xlsx .xlsm], 'an Excel 2007', file_warning, packed)
        basename = find_basename(filename_or_stream)
      end

      # NOTE: Create temp directory and allow Ruby to cleanup the temp directory
      #       when the object is garbage collected. Initially, the finalizer was
      #       created in the Roo::Tempdir module, but that led to a segfault
      #       when testing in Ruby 2.4.0.
      @tmpdir = self.class.make_tempdir(self, basename, options[:tmpdir_root])
      ObjectSpace.define_finalizer(self, self.class.finalize(object_id))

      @shared = Shared.new(@tmpdir, shared_options)
      @filename = local_filename(filename_or_stream, @tmpdir, packed)
      @source_path = @filename
      process_zipfile(@filename || filename_or_stream)

      @sheet_names = []
      @sheets = []
      @sheets_by_name = {}

      workbook.sheets.each_with_index do |sheet, index|
        next if options[:only_visible_sheets] && sheet['state'] == 'hidden'

        sheet_name = sheet['name']
        built_sheet = Sheet.new(sheet_name, @shared, index, sheet_options, kind: @sheet_kinds[index])
        @sheet_names << sheet_name
        @sheets_by_name[sheet_name] = built_sheet
        @sheets << built_sheet
      end

      if cell_max
        cell_count = ::Roo::Utils.num_cells_in_range(sheet_for(options.delete(:sheet)).dimensions)
        raise ExceedsMaxError.new("Excel file exceeds cell maximum: #{cell_count} > #{cell_max}") if cell_count > cell_max
      end

      super
    rescue
      self.class.finalize_tempdirs(object_id)
      raise
    end

    def method_missing(method, *args)
      if (label = workbook.defined_names[method.to_s])
        safe_send(sheet_for(label.sheet).cells[label.key], :value)
      else
        # call super for methods like #a1
        super
      end
    end

    def sheets
      @sheet_names
    end

    def sheet_for(sheet)
      sheet ||= default_sheet
      validate_sheet!(sheet)
      @sheets_by_name[sheet] || @sheets[sheet]
    end

    def images(sheet = nil)
      images_names = sheet_for(sheet).images.map(&:last)
      images_names.map { |iname| image_files.find { |ifile| ifile[iname] } }
    end

    # Builds a local export plan for the authorized sheets. The plan lists every
    # part the selected sheets actually reach, with its relationship owner,
    # resolved target, digest, and source. Parts belonging to unselected sheets
    # are never used to fill a gap.
    def export_plan(sheet_names:, scope: :complete)
      ensure_package
      Exporter.new(@package, sheet_names: sheet_names, scope: scope, source: source_path).plan
    end

    # Writes a local .xlsx containing only the authorized sheets and rereads it
    # cell by cell. See Roo::Excelx::Exporter for the complete vs strict scope.
    def export_sheets(destination, sheet_names:, scope: :complete)
      ensure_package
      exporter = Exporter.new(@package, sheet_names: sheet_names, scope: scope, source: source_path)
      plan = exporter.plan
      result = exporter.write(destination, plan)
      {
        plan: plan,
        write: result,
        audit: verify_export(destination, sheet_names: plan[:selected_sheets])
      }
    end

    def verify_export(destination, sheet_names:)
      if source_path
        Auditor.new(source_path, destination, sheet_names: sheet_names).report
      else
        {
          ok: nil,
          skipped: true,
          reason: 'the source was opened from a stream, so there is no source path to reread; reopen the source file path to run the cell-by-cell audit',
          copy: destination,
          sheets: Array(sheet_names)
        }
      end
    end

    # Returns the content of a spreadsheet-cell.
    # (1,1) is the upper left corner.
    # (1,1), (1,'A'), ('A',1), ('a',1) all refers to the
    # cell at the first line and first row.
    def cell(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :value)
    end

    def row(rownumber, sheet = nil)
      sheet_for(sheet).row(rownumber)
    end

    # returns all values in this column as an array
    # column numbers are 1,2,3,... like in the spreadsheet
    def column(column_number, sheet = nil)
      if column_number.is_a?(::String)
        column_number = ::Roo::Utils.letter_to_number(column_number)
      end
      sheet_for(sheet).column(column_number)
    end

    # returns the number of the first non-empty row
    def first_row(sheet = nil)
      sheet_for(sheet).first_row
    end

    # returns the number of the last non-empty row
    def last_row(sheet = nil)
      sheet_for(sheet).last_row
    end

    # returns the number of the first non-empty column
    def first_column(sheet = nil)
      sheet_for(sheet).first_column
    end

    # returns the number of the last non-empty column
    def last_column(sheet = nil)
      sheet_for(sheet).last_column
    end

    # set a cell to a certain value
    # (this will not be saved back to the spreadsheet file!)
    def set(row, col, value, sheet = nil) #:nodoc:
      key = normalize(row, col)
      cell_type = cell_type_by_value(value)
      sheet_for(sheet).cells[key] = Cell.new(value, cell_type, nil, cell_type, value, nil, nil, nil, Coordinate.new(row, col))
    end

    # Returns the formula at (row,col).
    # Returns nil if there is no formula.
    # The method #formula? checks if there is a formula.
    def formula(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :formula)
    end

    # Predicate methods really should return a boolean
    # value. Hopefully no one was relying on the fact that this
    # previously returned either nil/formula
    def formula?(*args)
      !!formula(*args)
    end

    # returns each formula in the selected sheet as an array of tuples in following format
    # [[row, col, formula], [row, col, formula],...]
    def formulas(sheet = nil)
      sheet_for(sheet).cells.select { |_, cell| cell.formula }.map do |(x, y), cell|
        [x, y, cell.formula]
      end
    end

    # Given a cell, return the cell's style
    def font(row, col, sheet = nil)
      key = normalize(row, col)
      definition_index = safe_send(sheet_for(sheet).cells[key], :style)
      styles.definitions[definition_index] if definition_index
    end

    # returns the type of a cell:
    # * :float
    # * :string,
    # * :date
    # * :percentage
    # * :formula
    # * :time
    # * :datetime
    def celltype(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :type)
    end

    # returns the internal type of an excel cell
    # * :numeric_or_formula
    # * :string
    # Note: this is only available within the Excelx class
    def excelx_type(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :cell_type)
    end

    # returns the internal value of an excelx cell
    # Note: this is only available within the Excelx class
    def excelx_value(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :cell_value)
    end

    # returns the internal value of an excelx cell
    # Note: this is only available within the Excelx class
    def formatted_value(row, col, sheet = nil)
      key = normalize(row, col)
      safe_send(sheet_for(sheet).cells[key], :formatted_value)
    end

    # returns the internal format of an excel cell
    def excelx_format(row, col, sheet = nil)
      key = normalize(row, col)
      sheet_for(sheet).excelx_format(key)
    end

    def empty?(row, col, sheet = nil)
      sheet = sheet_for(sheet)
      key = normalize(row, col)
      cell = sheet.cells[key]
      !cell || cell.empty? ||
        (row < sheet.first_row || row > sheet.last_row || col < sheet.first_column || col > sheet.last_column)
    end

    # shows the internal representation of all cells
    # for debugging purposes
    def to_s(sheet = nil)
      sheet_for(sheet).cells.inspect
    end

    # returns the row,col values of the labelled cell
    # (nil,nil) if label is not defined
    def label(name)
      labels = workbook.defined_names
      return [nil, nil, nil] if labels.empty? || !labels.key?(name)

      [labels[name].row, labels[name].col, labels[name].sheet]
    end

    # Returns an array which all labels. Each element is an array with
    # [labelname, [row,col,sheetname]]
    def labels
      @labels ||= workbook.defined_names.map do |name, label|
        [
          name,
          [label.row, label.col, label.sheet]
        ]
      end
    end

    def hyperlink?(row, col, sheet = nil)
      !!hyperlink(row, col, sheet)
    end

    # returns the hyperlink at (row/col)
    # nil if there is no hyperlink
    def hyperlink(row, col, sheet = nil)
      key = normalize(row, col)
      sheet_for(sheet).hyperlinks[key]
    end

    # returns the comment at (row/col)
    # nil if there is no comment
    def comment(row, col, sheet = nil)
      key = normalize(row, col)
      sheet_for(sheet).comments[key]
    end

    # true, if there is a comment
    def comment?(row, col, sheet = nil)
      !!comment(row, col, sheet)
    end

    def comments(sheet = nil)
      sheet_for(sheet).comments.map do |(x, y), comment|
        [x, y, comment]
      end
    end

    # Yield an array of Excelx::Cell
    # Takes options for sheet, pad_cells, and max_rows
    def each_row_streaming(options = {})
      sheet = sheet_for(options.delete(:sheet))
      if block_given?
        sheet.each_row(options) { |row| yield row }
      else
        sheet.to_enum(:each_row, options)
      end
    end

    private

    def ensure_package
      @package ||= begin
        zipfile = @filename || @source_path
        if is_stream?(zipfile)
          raise ExportError, 'export requires a file-backed workbook; open the file path to export selected sheets'
        end

        Package.new(zipfile)
      end
    end

    def clean_sheet(sheet)
      @sheets_by_name[sheet].cells.each_pair do |coord, value|
        next unless value.value.is_a?(::String)

        @sheets_by_name[sheet].cells[coord].value = sanitize_value(value.value)
      end

      @cleaned[sheet] = true
    end

    def extract_images(entry_names, tmpdir)
      image_names = entry_names.grep(%r{media/image([0-9]+)})
      image_names.each do |entry_name|
        content = @package.read(entry_name)
        next unless content

        path = "#{tmpdir}/roo#{entry_name.gsub(/xl\/|\//, '_')}"
        image_files << path
        File.binwrite(path, content)
      end
    end

    # Extracts all needed files from the zip file
    def process_zipfile(zipfilename_or_stream)
      @sheet_files = []

      if is_stream?(zipfilename_or_stream)
        zip_file = Zip::CentralDirectory.new
        zip_file.read_from_stream zipfilename_or_stream
      else
        zip_file = Zip::File.open(zipfilename_or_stream)
      end

      @package = Package.new(zip_file)
      process_zipfile_entries
    end

    def process_zipfile_entries
      # NOTE: When Google or Numbers 3.1 exports to xlsx, the worksheet filenames
      #       are not in order. With Numbers 3.1, the first sheet is always
      #       sheet.xml, not sheet1.xml. With Google, the order of the worksheets is
      #       independent of a worksheet's filename (i.e. sheet6.xml can be the
      #       first worksheet).
      #
      #       Every part is resolved through the relationship graph: workbook.xml
      #       names each sheet and workbook.xml.rels names its part; that sheet's
      #       own .rels then names its comments and drawing. Filename numbers and
      #       sheet positions are never used as a fallback.
      @sheet_kinds = []
      workbook_part = @package.workbook_part

      extract_package_part(workbook_part, "#{@tmpdir}/roo_workbook.xml")
      extract_package_part(@package.relationships_part_name(workbook_part), "#{@tmpdir}/roo_workbook.xml.rels")

      workbook_relationships = @package.relationships(workbook_part)
      shared_strings = workbook_relationships.find { |relationship| relationship.type_name == "sharedStrings" }
      styles = workbook_relationships.find { |relationship| relationship.type_name == "styles" }

      extract_package_part(shared_strings.resolved_target, "#{@tmpdir}/roo_sharedStrings.xml") if shared_strings
      extract_package_part(styles.resolved_target, "#{@tmpdir}/roo_styles.xml") if styles

      @package.sheets.each_with_index do |sheet, index|
        @sheet_kinds[index] = sheet.kind
        next unless sheet.part && sheet.relationship && !sheet.relationship.external?

        sheet_path = "#{@tmpdir}/roo_sheet#{index + 1}"
        extract_package_part(sheet.part, sheet_path)
        sheet_files[index] = sheet_path
        @sheet_files << sheet_path

        sheet_rels_part = @package.relationships_part_name(sheet.part)
        rels_path = "#{@tmpdir}/roo_rels#{index + 1}"
        extract_package_part(sheet_rels_part, rels_path)
        rels_files[index] = rels_path if @package.exist?(sheet_rels_part)

        sheet_relationships = @package.relationships(sheet.part)

        comments = sheet_relationships.find { |relationship| relationship.type_name == "comments" }
        if comments && !comments.external?
          comments_path = "#{@tmpdir}/roo_comments#{index + 1}"
          extract_package_part(comments.resolved_target, comments_path)
          comments_files[index] = comments_path
        end

        drawing = sheet_relationships.find { |relationship| relationship.type_name == "drawing" }
        next unless drawing && !drawing.external?

        drawing_rels = @package.relationships_part_name(drawing.resolved_target)
        next unless @package.exist?(drawing_rels)

        image_rels_path = "#{@tmpdir}/roo_image_rels#{index + 1}"
        extract_package_part(drawing_rels, image_rels_path)
        image_rels[index] = image_rels_path
      end

      extract_images(@package.names, @tmpdir)
    end

    def extract_package_part(part_name, destination)
      return nil unless part_name

      content = @package.read(part_name)
      return nil unless content

      File.binwrite(destination, content)
      destination
    end

    def safe_send(object, method, *args)
      object.send(method, *args) if object&.respond_to?(method)
    end
  end
end
