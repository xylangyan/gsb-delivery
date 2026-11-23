# frozen_string_literal: true

require 'zlib'

# Minimal ZIP writer used only to create the test fixtures with exact,
# known layouts. It is deliberately independent of the editor under test.
module FixtureZip
  module_function

  # extra: raw extra-field bytes to place in BOTH the local and central
  # headers unless :local_extra / :central_extra are given.
  def build(entries, comment: '')
    out = +''.b
    central = +''.b
    entries.each do |spec|
      name = spec.fetch(:name)
      data = spec.fetch(:data)
      crc = Zlib.crc32(data)
      deflated = deflate(data)
      local_extra = spec.fetch(:local_extra, spec.fetch(:extra, ''))
      central_extra = spec.fetch(:central_extra, spec.fetch(:extra, ''))
      local_offset = out.bytesize

      flags = spec.fetch(:flags, 0)
      out << [0x04034b50, 20, flags, 8, 0, 0, crc, deflated.bytesize, data.bytesize,
              name.bytesize, local_extra.bytesize].pack('VvvvvvVVVvv')
      out << name.b << local_extra << deflated

      central << [0x02014b50, 20, 20, flags, 8, 0, 0, crc, deflated.bytesize, data.bytesize,
                  name.bytesize, central_extra.bytesize, spec.fetch(:comment, '').bytesize,
                  0, 0, 0, local_offset].pack('VvvvvvvVVVvvvvvVV')
      central << name.b << central_extra << spec.fetch(:comment, '').b
    end

    cdir_offset = out.bytesize
    out << central
    out << [0x06054b50, 0, 0, entries.size, entries.size, central.bytesize,
            cdir_offset, comment.bytesize].pack('VvvvvVVv')
    out << comment.b
  end

  def deflate(data)
    z = Zlib::Deflate.new(Zlib::DEFAULT_COMPRESSION, -Zlib::MAX_WBITS)
    z.deflate(data, Zlib::FINISH).tap { z.close }
  end

  # A structurally valid private extra field of exactly `total` bytes.
  def padding(total, id: 0xCAFE, fill: 0x00)
    return ''.b if total.zero?
    raise ArgumentError, 'padding must be at least 4 bytes' if total < 4

    [id].pack('v') + [total - 4].pack('v') + ([fill].pack('C') * (total - 4))
  end

  def pad_to(total, id: 0xCAFE)
    padding(total, id: id)
  end
end
