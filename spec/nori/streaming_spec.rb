require "spec_helper"
require "tmpdir"

describe Nori::Streaming do
  let(:daily_xml) do
    <<~XML
      <daily>
        <warehouse code="W1">
          <record id="1"><name>alpha</name><body>first</body></record>
          <record id="2"><name>beta</name><body>second</body></record>
        </warehouse>
        <warehouse code="W2">
          <record id="3"><name>gamma</name><body>third</body></record>
        </warehouse>
      </daily>
    XML
  end

  def session_for(options = {})
    Nori::Streaming::Session.new({ :records => ["record"] }.merge(options))
  end

  describe "incremental delivery" do
    it "emits a record as soon as its closing tag arrives, before later data" do
      session = session_for
      delivered = []
      session.feed('<daily><warehouse><record id="1"><name>alpha</name></record>') do |record|
        delivered << record.value
      end
      expect(delivered).to eq(["name" => "alpha", "@id" => "1"])

      session.feed('<record id="2"><name>beta</name></record></warehouse></daily>')
      expect(session.records.map(&:value)).to eq(
        [
          { "name" => "alpha", "@id" => "1" },
          { "name" => "beta", "@id" => "2" }
        ]
      )
    end

    it "delivers records from the same feed in the order their tags close" do
      records = session_for.feed(daily_xml)
      expect(records.map { |record| record.value["name"] }).to eq(%w[alpha beta gamma])
    end

    it "keeps attachment and body values exactly as a full parse would" do
      xml = <<~XML
        <daily>
          <record id="1">
            <body>line one
        line two</body>
            <attachment type="file" name="report.txt" content_type="text/plain">aGVsbG8=</attachment>
          </record>
        </daily>
      XML

      record = session_for.feed(xml).first.value
      full = Nori.new.parse(xml)["daily"]["record"]

      expect(record["body"]).to eq(full["body"])
      expect(record["attachment"].read).to eq("hello")
      expect(record["attachment"].original_filename).to eq("report.txt")
      expect(record["attachment"].content_type).to eq("text/plain")
    end

    it "does not parse or assign an id to a record that has not closed" do
      session = session_for
      expect(session.feed('<daily><record id="1"><name>alpha</name>')).to eq([])
      expect(session.ledger).to be_empty
      expect(session.progress[:pending]).to be(true)
      expect(session.progress[:open_record][:name]).to eq("record")
    end
  end

  describe "boundaries" do
    it "selects commit points by raw element name" do
      xml = '<daily><record>a</record><ledger>b</ledger><record>c</record></daily>'
      records = session_for(:records => ["record"]).feed(xml)
      expect(records.map(&:value)).to eq(%w[a c])
    end

    it "selects commit points by directory hierarchy" do
      xml = <<~XML
        <daily>
          <warehouse><record>in</record></warehouse>
          <archive><record>out</record></archive>
        </daily>
      XML

      session = Nori::Streaming::Session.new(
        :records => [{ :path => %w[daily warehouse record] }]
      )
      records = session.feed(xml)
      expect(records.map(&:value)).to eq(["in"])
      expect(records.first.path).to eq(%w[daily warehouse record])
    end

    it "matches raw names before namespace stripping or key conversion" do
      xml = '<daily xmlns:p="urn:warehouse"><p:record id="1">a</p:record></daily>'
      session = Nori::Streaming::Session.new(
        :records => ["p:record"],
        :strip_namespaces => true
      )
      expect(session.feed(xml).map(&:value)).to eq(["a"])

      session = Nori::Streaming::Session.new(:records => ["record"], :strip_namespaces => true)
      expect(session.feed(xml)).to eq([])
    end

    it "keeps same-named tags distinct even though Nori folds normalized keys together" do
      # With strip_namespaces the two prefixes collapse to the same Nori key.
      # Boundary selection still sees the raw names and does not confuse them.
      xml = <<~XML
        <daily>
          <p:record xmlns:p="urn:a">one</p:record>
          <q:record xmlns:q="urn:b">two</q:record>
        </daily>
      XML

      p_session = Nori::Streaming::Session.new(:records => ["p:record"], :strip_namespaces => true)
      q_session = Nori::Streaming::Session.new(:records => ["q:record"], :strip_namespaces => true)

      expect(p_session.feed(xml).map(&:value)).to eq(["one"])
      expect(q_session.feed(xml).map(&:value)).to eq(["two"])
    end

    it "supports nested boundaries, delivering the inner record first" do
      xml = '<daily><shipment id="s1"><record id="r1">a</record></shipment></daily>'
      session = Nori::Streaming::Session.new(
        :records => [{ :name => "shipment" }, { :name => "record" }]
      )
      expect(session.feed(xml).map(&:name)).to eq(%w[record shipment])
    end
  end

  describe "business ids" do
    it "assigns one unique id per original record" do
      records = session_for.feed(daily_xml)
      ids = records.map(&:id)
      expect(ids.uniq.length).to eq(3)
      expect(ids.compact.length).to eq(3)
    end

    it "looks a record back up by its id in the receive ledger" do
      session = session_for
      delivered = session.feed(daily_xml)
      delivered.each do |record|
        expect(session.ledger[record.id].byte_start).to eq(record.byte_start)
        expect(session.ledger[record.id].byte_end).to eq(record.byte_end)
      end
    end

    it "lets the conversion callback use only the current record, fixed options and the id" do
      seen = []
      converter = lambda { |tag, business_id| seen << [tag, business_id]; tag }
      session = session_for(:convert_tags_to => converter)
      records = session.feed('<daily><record id="1"><name>a</name></record><record id="2"><name>b</name></record></daily>')

      records.each do |record|
        conversions = seen.select { |_, id| id == record.id }
        expect(conversions).not_to be_empty
        expect(conversions.map(&:first)).to include("record", "name")
      end
      expect(seen.map(&:last).compact.uniq.sort).to eq(records.map(&:id).sort)
    end

    it "never mints an id before the record closes" do
      session = session_for
      session.feed('<daily><warehouse><record id="1"><name>alpha</name></record>')
      session.feed('<record id="2"><name>beta</name>')
      expect(session.ledger.size).to eq(1)
      expect(session.ledger.entries.map(&:status)).to eq([:delivered])
    end

    it "keeps ids stable when a checkpoint is reloaded" do
      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        first = session.feed('<daily><record id="1">a</record>')
        session.checkpoint!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(restored.records.map(&:id)).to eq(first.map(&:id))
        expect(restored.recover.map(&:id)).to eq(first.map(&:id))
      end
    end
  end

  describe "acknowledgements" do
    it "treats a late acknowledgement as belonging to the original record" do
      session = session_for
      records = session.feed(daily_xml)
      # acknowledge the first after all three have been delivered
      session.acknowledge(records.first)
      expect(session.ledger[records.first.id].status.to_s).to eq("acknowledged")
      expect(session.ledger[records[1].id].status.to_s).to eq("delivered")
    end

    it "accepts the same acknowledgement more than once" do
      session = session_for
      record = session.feed(daily_xml).first
      session.acknowledge(record)
      session.acknowledge(record.id)
      expect(session.ledger[record.id].status.to_s).to eq("acknowledged")
    end

    it "raises for an unknown receipt instead of acknowledging the wrong record" do
      session = session_for
      session.feed(daily_xml)
      expect { session.acknowledge("not-a-real-id") }.to raise_error(Nori::Streaming::UnknownReceipt)
    end

    it "does not report acknowledged records as pending after recovery" do
      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        records = session.feed(daily_xml)
        session.acknowledge(records[0])
        session.acknowledge(records[1])
        session.checkpoint!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(restored.recover.map(&:id)).to eq([records[2].id])
      end
    end
  end

  describe "reclaiming confirmed bytes" do
    it "never discards bytes needed by an open or unacknowledged record" do
      xml = '<daily><record id="1">a</record><record id="2">b</record></daily>'
      session = session_for
      records = session.feed(xml)
      session.acknowledge(records.first)

      expect(session.safe_discard_offset).to eq(records[1].byte_start)
      session.discard_acknowledged!
      expect(session.retained_from).to eq(records[1].byte_start)
    end

    it "resumes after the upstream deletes every acknowledged byte" do
      xml = '<daily><record id="1">a</record><record id="2">b</record><record id="3">c</record></daily>'
      split = xml.index('<record id="2"')

      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        first = session.feed(xml[0...split])
        session.acknowledge(first.first)
        session.discard_acknowledged!
        session.checkpoint!
        expect(session.retained_from).to eq(split)

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        remaining = restored.feed(xml[split..-1])
        expect(remaining.map(&:value)).to eq(%w[b c])
        expect(remaining.map(&:byte_start).first).to eq(split)
      end
    end

    it "rejects a feed that leaves a gap instead of guessing" do
      session = session_for
      session.feed('<daily><record >a</record>')
      expect { session.feed("<record>b</record></daily>", :offset => 100) }.
        to raise_error(Nori::Streaming::OffsetError)
    end
  end

  describe "pausing inside a record" do
    it "resumes a pause inside CDATA while the previous receipt is still unacknowledged" do
      xml = <<~XML
        <daily>
          <record id="1"><body>first</body></record>
          <record id="2"><body><![CDATA[hello world]]></body></record>
        </daily>
      XML

      pause = xml.index("hello") + 3
      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        first = session.feed(xml[0...pause])
        expect(first.map(&:value)).to eq(["body" => "first", "@id" => "1"])
        expect(session.ledger[first.first.id].status.to_s).to eq("delivered")

        session.checkpoint!
        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        records = restored.feed(xml[pause..-1])

        expect(records.map(&:value)).to eq(["body" => "hello world", "@id" => "2"])
        expect(restored.recover.map(&:id)).to eq(first.map(&:id) + records.map(&:id))
      end
    end

    it "matches a full parse from every possible split point" do
      xml = '<daily><record id="1"><v>one</v></record>' \
            '<record id="2"><v><![CDATA[hello]]></v><f type="file" name="a">aGk=</f></record>' \
            '<record id="3"><v>three</v></record></daily>'
      expected = Nori.new.parse(xml)["daily"]["record"].map { |value| normalize_value(value) }

      (0..xml.bytesize).each do |cut|
        session = session_for
        values = session.feed(xml.byteslice(0, cut)) + session.feed(xml.byteslice(cut, xml.bytesize - cut))
        expect(values.map { |record| normalize_value(record.value) }).to eq(expected), "split at #{cut}"
      end
    end

    it "keeps a large attachment whole across repeated pauses" do
      payload = "QUJD" * 20_000
      xml = %Q{<daily><record id="1"><f type="file" name="x">#{payload}</f></record></daily>}

      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        closing = xml.index("</record>")
        cuts = [64, 1024, 65_536, payload.bytesize, closing]
        offset = 0
        cuts.each do |cut|
          session.feed(xml[offset...cut])
          session.checkpoint!
          session = Nori::Streaming::Session.load(session.checkpoint_path)
          offset = cut
        end
        record = session.feed(xml[offset..-1]).first
        expect(record.value["f"].read.bytesize).to eq(60_000)
      end
    end

    it "survives repeated pauses inside one record's attachment" do
      payload = "A" * 512
      xml = %Q{<daily><record id="1"><f type="file" name="x">#{payload}</f></record></daily>}

      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        cuts = [20, 50, 120, 300, xml.bytesize - 10]
        offset = 0
        cuts.each do |cut|
          session.feed(xml[offset...cut])
          session.checkpoint!
          session = Nori::Streaming::Session.load(session.checkpoint_path)
          offset = cut
        end
        record = session.feed(xml[offset..-1]).first
        # 512 base64 characters decode to 384 bytes.
        expect(record.value["f"].read.bytesize).to eq(384)
      end
    end

    it "keeps inherited xml:space when the record redeclares only its prefix" do
      xml = <<~XML
        <daily xml:space="preserve" xmlns:p="urn:warehouse">
          <p:record xmlns:p="urn:other"><p:note>  keep  </p:note></p:record>
        </daily>
      XML

      record = session_for(:records => ["p:record"], :standards => true).feed(xml).first
      expect(record.value["p:note"]).to eq("  keep  ")
    end

    it "keeps the inherited prefix when the record redeclares only xml:space" do
      xml = <<~XML
        <daily xml:space="preserve" xmlns:p="urn:warehouse">
          <p:record xml:space="default"><p:note>   </p:note></p:record>
        </daily>
      XML

      record = session_for(:records => ["p:record"], :standards => true).feed(xml).first
      expect(record.value["p:note"]).to eq("")
    end

    it "does not reread reclaimed bytes to build a later record" do
      xml = '<daily><record id="1">a</record><record id="2">b</record></daily>'
      split = xml.index('<record id="2"')

      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        first = session.feed(xml[0...split])
        session.acknowledge(first.first)
        session.discard_acknowledged!
        session.checkpoint!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(restored.retained_from).to eq(split)
        expect { restored.spool.bytes(0, 1) }.to raise_error(Nori::Streaming::OffsetError)
        expect(restored.feed(xml[split..-1]).first.raw).to eq('<record id="2">b</record>')
      end
    end
  end

  describe "credentials" do
    it "reports the exact original byte range of each record" do
      session = session_for
      records = session.feed(daily_xml)

      records.each do |record|
        expect(daily_xml.b[record.byte_start...record.byte_end]).to start_with('<record id="')
        expect(daily_xml.b[record.byte_start...record.byte_end]).to end_with("</record>")
        expect(record.credential[:byte_range]).to eq(record.byte_start..(record.byte_end - 1))
      end
    end

    it "keeps absolute ranges after recovery from a reclaimed window" do
      xml = '<daily><record id="1">a</record><record id="2">b</record></daily>'
      split = xml.index('<record id="2"')

      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        first = session.feed(xml[0...split])
        session.checkpoint!
        session.acknowledge(first.first)
        session.discard_acknowledged!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        second = restored.feed(xml[split..-1]).first
        expect(second.byte_start).to eq(split)
        expect(xml.b[second.byte_start...second.byte_end]).to eq('<record id="2">b</record>')
      end
    end

    it "reports precisely how far unclosed bytes have been stored" do
      xml = '<daily><record id="1"><body>partial'
      session = session_for
      session.feed(xml)

      expect(session.stored_through).to eq(xml.bytesize)
      expect(session.next_offset).to eq(xml.bytesize)
      expect(session.progress[:stored_through]).to eq(xml.bytesize)
      expect(session.progress[:open_record][:byte_start]).to eq(xml.index("<record"))
      expect(session.ledger).to be_empty
    end

    it "raises with the stored offset when the transmission ends inside a record" do
      session = session_for
      session.feed('<daily><record id="1"><body>partial')
      expect { session.finish }.to raise_error(Nori::Streaming::ParseError, /bytes stored through \d+/)
      expect(session.ledger).to be_empty
    end
  end

  describe "changing conversion options" do
    it "applies new options only to records that have not started" do
      xml = '<daily><record id="1"><name>one</name></record><record id="2"><name>two</name></record></daily>'
      session = session_for(:convert_tags_to => lambda { |tag| tag })
      split = xml.index('<record id="2"')
      session.feed(xml[0...split])

      session.options = { :convert_tags_to => lambda { |tag| tag.upcase } }
      second = session.feed(xml[split..-1]).first

      expect(session.records.first.value).to eq("name" => "one", "@id" => "1")
      expect(second.value).to eq("NAME" => "two", "@ID" => "2")
    end

    it "keeps an open record on the options it started with" do
      xml = '<daily><record id="1"><name>one</name></record></daily>'
      session = session_for(:convert_tags_to => lambda { |tag| tag })
      session.feed('<daily><record id="1">')
      session.options = { :convert_tags_to => lambda { |tag| tag.upcase } }
      record = session.feed('<name>one</name></record></daily>').first

      expect(record.value).to eq("name" => "one", "@id" => "1")
    end
  end

  describe "checkpoint files" do
    it "restores the last unconfirmed record and everything after it" do
      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        records = session.feed(daily_xml)
        session.acknowledge(records[0])
        session.checkpoint!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(restored.recover.map(&:id)).to eq(records[1..2].map(&:id))
        expect(restored.recover.map(&:credential).map { |c| c[:byte_range] }).
          to eq(records[1..2].map(&:byte_range))
      end
    end

    it "replays a value produced with a converter that cannot be serialized" do
      Dir.mktmpdir do |dir|
        converter = lambda { |tag| "X_#{tag}" }
        session = session_for(:spool => dir, :convert_tags_to => converter)
        session.feed(daily_xml)
        session.checkpoint!

        restored = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(restored.records.first.value).to eq("X_name" => "alpha", "X_body" => "first", "X_@id" => "1")
      end
    end

    it "is idempotent when the same checkpoint is loaded twice" do
      Dir.mktmpdir do |dir|
        session = session_for(:spool => dir)
        records = session.feed(daily_xml)
        session.checkpoint!

        first = Nori::Streaming::Session.load(session.checkpoint_path)
        second = Nori::Streaming::Session.load(session.checkpoint_path)
        expect(first.recover.map(&:id)).to eq(second.recover.map(&:id))
        expect(first.recover.map(&:id)).to eq(records.map(&:id))
      end
    end
  end

  describe "per-record option providers" do
    it "asks the provider for options when each record starts" do
      provider = lambda do |context|
        context[:sequence].zero? ? { :advanced_typecasting => false } : {}
      end
      session = Nori::Streaming::Session.new(:records => ["record"], :option_provider => provider)
      values = session.feed('<daily><record><v>true</v></record><record><v>true</v></record></daily>').map(&:value)
      expect(values).to eq([{ "v" => "true" }, { "v" => true }])
    end
  end

  describe "IO sources" do
    it "reads an IO in chunks and emits records as they close" do
      require "stringio"
      io = StringIO.new('<daily><record id="1">a</record><record id="2">b</record></daily>')
      session = session_for
      records = session.feed_io(io, :chunk_size => 7, :final => true)
      expect(records.map(&:value)).to eq(["a", "b"])
      expect(session.finish).to eq([])
    end
  end

  describe "compatibility" do
    it "leaves Nori#parse unchanged for a complete document" do
      expect(Nori.new.parse('<daily><record>a</record></daily>')).
        to eq("daily" => { "record" => "a" })
    end

    it "can be built through Nori.stream" do
      expect(Nori.stream(:records => ["record"])).to be_a(Nori::Streaming::Session)
    end

    it "accepts the caller's parser option" do
      [:nokogiri, :rexml].each do |parser|
        session = Nori::Streaming::Session.new(:records => ["record"], :parser => parser)
        expect(session.feed(daily_xml).map(&:value).first["name"]).to eq("alpha")
      end
    end
  end

  def normalize_value(value)
    case value
    when Hash
      value.each_with_object({}) { |(key, entry), hash| hash[key] = normalize_value(entry) }
    when Array
      value.map { |entry| normalize_value(entry) }
    when Nori::StringIOFile
      [value.string, value.original_filename, value.content_type]
    else
      value
    end
  end
end
