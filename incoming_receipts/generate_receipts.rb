# Generates the sample batch of purchase receipts (receipts.pdf).
#
# The table template is a Form XObject (/FmTable) that itself references a
# smaller Form XObject (/FmCell). The same template object is painted twice on
# page 1 (once fully exposed at the top, once behind an outer clipping
# rectangle) and once more on page 2, so every invocation has to be accounted
# for separately.
#
# Paint-order scenarios on page 1:
#   * signature digits painted AFTER a white rectangle at the same spot
#     (rectangle is behind the digits, digits stay readable)
#   * one field whose character is covered only by the UNION of two later
#     white rectangles (neither covers it alone)
#   * one field whose digits are fully covered and then rewritten with a
#     complete replacement value on top
#
# Page 2 additionally paints an image XObject across a field to demonstrate
# flagging of drawing capabilities outside the declared scope.

require 'hexapdf'

OUTPUT = File.join(__dir__, 'receipts.pdf')

doc = HexaPDF::Document.new

font = doc.add({Type: :Font, Subtype: :Type1, BaseFont: :Helvetica,
                Encoding: :WinAnsiEncoding})

# Small nested table ("小表格"): its own BBox [0,0,12,16] cuts through the
# second character, and it carries its own clipping rectangle on the stack.
cell = doc.add({Type: :XObject, Subtype: :Form, BBox: [0, 0, 12, 16],
                Resources: {Font: {F1: font}}},
               stream: <<~PDF)
                 q
                 1 1 14 15 re W n
                 BT /F1 10 Tf 4 5 Td (X7) Tj ET
                 Q
                 0 0 12 16 re S
               PDF

# Main table template ("套表格再引用小表格").
table = doc.add({Type: :XObject, Subtype: :Form, BBox: [0, 0, 300, 40],
                 Resources: {Font: {F1: font}, XObject: {FmCell: cell}}},
                stream: <<~PDF)
                  0 0 300 40 re S
                  BT /F1 12 Tf 10 24 Td (PO-2026-0001) Tj ET
                  q
                  1 0 0 1 10 4 cm
                  /FmCell Do
                  Q
                  BT /F1 10 Tf 200 8 Td (NET 30) Tj ET
                PDF

# 8x8 gray image used for the undeclared-capability demonstration.
image = doc.add({Type: :XObject, Subtype: :Image, Width: 8, Height: 8,
                 ColorSpace: :DeviceGray, BitsPerComponent: 8},
                stream: ("\x7f".b * 64))

page1 = doc.pages.add
page1[:MediaBox] = [0, 0, 595, 842]
page1[:Resources] = {Font: {F1: font}, XObject: {FmTable: table}}
page1.contents = <<~PDF
  1 1 1 rg
  400 700 60 14 re f
  0 0 0 rg
  BT /F1 12 Tf 404 704 Td (9102) Tj ET
  q
  1 0 0 1 50 760 cm
  /FmTable Do
  Q
  q
  50 620 14 40 re W n
  1 0 0 1 50 620 cm
  /FmTable Do
  Q
  BT /F1 12 Tf 50 500 Td (AB12) Tj ET
  1 1 1 rg
  66.008 496 4 16 re f
  69.508 496 3.172 16 re f
  0 0 0 rg
  BT /F1 12 Tf 50 450 Td (OLD99) Tj ET
  1 1 1 rg
  48 446 42 18 re f
  0 0 0 rg
  BT /F1 12 Tf 50 450 Td (NEW01) Tj ET
PDF

page2 = doc.pages.add
page2[:MediaBox] = [0, 0, 595, 842]
page2[:Resources] = {Font: {F1: font}, XObject: {FmTable: table, Im1: image}}
page2.contents = <<~PDF
  q
  1 0 0 1 50 700 cm
  /FmTable Do
  Q
  BT /F1 12 Tf 50 600 Td (NOTE) Tj ET
  q
  40 0 0 14 48 596 cm
  /Im1 Do
  Q
PDF

doc.write(OUTPUT)
puts "written #{OUTPUT}"
