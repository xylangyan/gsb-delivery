require 'rspec'
require 'tempfile'
require '../lib/parseconfig.rb'

describe 'HotParseConfig' do
  # The rules document from the operations story: colon separated, '!' comments.
  STORY_LINES = [
    'route: office',
    '[west]',
    'route: main',
    '[east] ! 夜班',
    'route: spare',
    '[west]',
    'cursor: W',
    '[east]',
    'cursor: E'
  ].freeze

  def write_temp(lines)
    file = Tempfile.new(['hot', '.conf'])
    file.write(lines.join("\n") + "\n")
    file.flush
    file
  end

  def new_hot(lines = STORY_LINES)
    @tempfiles ||= []
    @tempfiles << (file = write_temp(lines))
    HotParseConfig.new(file.path, ':', ['!'])
  end

  def fresh_parse(lines)
    @tempfiles ||= []
    @tempfiles << (file = write_temp(lines))
    ParseConfig.new(file.path, ':', ['!'])
  end

  # Reference line-number map for a document, computed with a plain scan.
  def expected_lines(lines)
    global_line = {}
    group_key_line = {}
    first_header = {}
    group = nil
    lines.each_with_index do |raw, index|
      line = raw.strip
      lineno = index + 1
      next if line.start_with?('!')

      if line =~ /\s*:\s*/
        name = line.split(/\s*:\s*/, 2).first.strip
        if group
          group_key_line[[group, name]] = lineno
        else
          global_line[name] = lineno
        end
      elsif (match = /^\[(.+)\](\s*[!]+.*)?$/.match(line))
        group = match[1]
        first_header[group] ||= lineno
      elsif line =~ /\w+/
        global_line[line.strip] = lineno
      end
    end
    [global_line, group_key_line, first_header]
  end

  def expect_equivalent(hot, config, lines)
    expect(hot.get_groups).to eq(config.get_groups)
    expect(hot.get_params).to eq(config.get_params)
    expect(hot.params).to eq(config.params)
    expect(hot.params.keys).to eq(config.params.keys)

    config.get_params.each do |name|
      expect(hot[name]).to eq(config[name])
    end

    global_line, group_key_line, first_header = expected_lines(lines)
    config.get_groups.each do |group|
      next unless config[group].is_a?(Hash)

      expect(hot.line_of(group)).to eq(first_header[group])
      config[group].each_key do |key|
        expect(hot[group][key]).to eq(config[group][key])
        expect(hot.line_of(group, key)).to eq(group_key_line[[group, key]])
      end
    end
    config.get_params.each do |name|
      next if config[name].is_a?(Hash)

      expect(hot.line_of(name)).to eq(global_line[name])
    end
  end

  describe 'initial full read' do
    it 'matches ParseConfig on values, types and group order' do
      hot = new_hot
      config = fresh_parse(STORY_LINES)

      expect(hot['route']).to eq('office')
      expect(hot['west']['route']).to eq('main')
      expect(hot['east']['route']).to eq('spare')
      expect(hot['west']['cursor']).to eq('W')
      expect(hot['east']['cursor']).to eq('E')
      expect(hot.get_groups).to eq(%w[west east])
      expect_equivalent(hot, config, STORY_LINES)
    end

    it 'reports physical line numbers of matched rules' do
      hot = new_hot
      expect(hot.line_of('route')).to eq(1)
      expect(hot.line_of('east', 'route')).to eq(5)
      expect(hot.line_of('west', 'cursor')).to eq(7)
      expect(hot.line_of('east', 'cursor')).to eq(9)
      expect(hot.line_of('missing')).to be_nil
      expect(hot.line_of('east', 'missing')).to be_nil
    end
  end

  describe 'batch publishing' do
    it 'renames a group header with a single-line replacement' do
      hot = new_hot
      hot.publish([{ op: :replace, line: 4, text: '[north] ! 夜班' }])

      expect(hot['north']['route']).to eq('spare')
      expect(hot.line_of('north', 'route')).to eq(5)
      expect(hot['east']['route']).to be_nil
      expect(hot['east']['cursor']).to eq('E')
      expect(hot.get_groups).to eq(%w[west north east])
    end

    it 'adds a global boolean flag with a single inserted bare line' do
      hot = new_hot
      hot.publish([{ op: :insert, before: 4, text: 'north' }])

      expect(hot['north']).to be(true)
      expect(hot.line_of('north')).to eq(4)
      expect(hot['west']['route']).to eq('main')
      expect(hot['east']['route']).to eq('spare')
      expect(hot.get_groups).to eq(%w[west east])
    end

    it 'rejects a batch whose flag and group header collide, keeping the current version' do
      hot = new_hot
      expect do
        hot.publish([
          { op: :insert, before: 4, text: 'north' },
          { op: :replace, line: 4, text: '[north] ! 夜班' }
        ])
      end.to raise_error(HotParseConfig::ConflictError, /north.*line 4.*line 5/)

      expect(hot['east']['route']).to eq('spare')
      expect(hot.line_of('east', 'route')).to eq(5)
      expect(hot['north']).to be_nil
      expect(hot.get_groups).to eq(%w[west east])
    end

    it 'publishes the corrected batch and matches a fresh full read' do
      hot = new_hot
      hot.publish([
        { op: :insert, before: 4, text: 'failover' },
        { op: :replace, line: 4, text: '[north] ! 夜班' }
      ])

      expect(hot['north']['route']).to eq('spare')
      expect(hot.line_of('north', 'route')).to eq(6)
      expect(hot['east']['route']).to be_nil
      expect(hot['east']['cursor']).to eq('E')
      expect(hot['west']['route']).to eq('main')
      expect(hot['west']['cursor']).to eq('W')
      expect(hot['failover']).to be(true)
      expect(hot.line_of('failover')).to eq(4)

      candidate = [
        'route: office', '[west]', 'route: main', 'failover', '[north] ! 夜班',
        'route: spare', '[west]', 'cursor: W', '[east]', 'cursor: E'
      ]
      expect_equivalent(hot, fresh_parse(candidate), candidate)
    end

    it 'can delete a previously published flag, rename and add another flag in one batch' do
      hot = new_hot
      hot.publish([{ op: :insert, before: 4, text: 'north' }])
      expect(hot['north']).to be(true)

      hot.publish([
        { op: :delete, line: 4 },
        { op: :replace, line: 5, text: '[north] ! 夜班' },
        { op: :insert, before: 4, text: 'failover' }
      ])

      candidate = [
        'route: office', '[west]', 'route: main', 'failover', '[north] ! 夜班',
        'route: spare', '[west]', 'cursor: W', '[east]', 'cursor: E'
      ]
      expect_equivalent(hot, fresh_parse(candidate), candidate)
      expect(hot['north']['route']).to eq('spare')
      expect(hot['failover']).to be(true)
    end

    it 'supports deletions and appends' do
      hot = new_hot
      hot.publish([
        { op: :delete, line: 9 },
        { op: :insert, before: 10, text: 'trailing: yes' }
      ])

      expect(hot['east']['cursor']).to be_nil
      expect(hot['east']['trailing']).to eq('yes')
      expect(hot.line_of('east', 'trailing')).to eq(9)
    end

    it 'keeps submission order for multiple inserts at the same position' do
      hot = new_hot
      hot.publish([
        { op: :insert, before: 1, text: 'alpha: 1' },
        { op: :insert, before: 1, text: 'beta: 2' }
      ])

      expect(hot.get_params.first(2)).to eq(%w[alpha beta])
      expect(hot.line_of('alpha')).to eq(1)
      expect(hot.line_of('beta')).to eq(2)
      expect(hot.line_of('route')).to eq(3)
    end

    it 'rejects malformed edits without touching the current version' do
      hot = new_hot
      expect { hot.publish([{ op: :delete, line: 99 }]) }
        .to raise_error(ArgumentError, /out of range/)
      expect { hot.publish([{ op: :replace, line: 0, text: 'x' }]) }
        .to raise_error(ArgumentError, /out of range/)
      expect { hot.publish([{ op: :insert, before: 99, text: 'x' }]) }
        .to raise_error(ArgumentError, /out of range/)
      expect { hot.publish([{ op: :delete, line: 2 }, { op: :replace, line: 2, text: 'x' }]) }
        .to raise_error(ArgumentError, /more than once/)
      expect { hot.publish([{ op: :bogus, line: 1 }]) }
        .to raise_error(ArgumentError, /unknown edit op/)
      expect { hot.publish([{ op: :insert, before: 1 }]) }
        .to raise_error(ArgumentError, /requires :text/)

      expect(hot.get_groups).to eq(%w[west east])
      expect(hot['east']['route']).to eq('spare')
    end

    it 'validates a candidate through preview without switching versions' do
      hot = new_hot
      west_before = hot['west']

      candidate = hot.preview([{ op: :replace, line: 4, text: '[north] ! 夜班' }])
      expect(candidate['north']['route']).to eq('spare')
      expect(candidate.line_of('north', 'route')).to eq(5)

      # the collector keeps reading the current version
      expect(hot['east']['route']).to eq('spare')
      expect(hot['north']).to be_nil

      hot.publish([{ op: :replace, line: 4, text: '[north] ! 夜班' }])
      expect(hot['north']['route']).to eq('spare')

      # snapshots handed out before the publish stay valid
      expect(west_before['route']).to eq('main')
    end

    it 'starts empty and accepts inserts' do
      hot = HotParseConfig.new(nil, ':', ['!'])
      expect(hot.get_params).to eq([])

      hot.publish([
        { op: :insert, before: 1, text: '[only]' },
        { op: :insert, before: 1, text: 'key: value' }
      ])
      expect(hot['only']['key']).to eq('value')
      expect(hot.line_of('only', 'key')).to eq(2)
    end
  end

  describe 'large groups' do
    it 'renames a header over a huge section without scanning its rules' do
      count = 100_000
      lines = ['route: office', '[west]', 'route: main', '[east] ! 夜班', 'route: spare']
      count.times { |i| lines << "item#{i}: value#{i}" }
      lines += ['[west]', 'cursor: W', '[east]', 'cursor: E']
      hot = new_hot(lines)

      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      hot.publish([{ op: :replace, line: 4, text: '[north] ! 夜班' }])
      expect(hot['north']['item0']).to eq('value0')
      expect(hot['north']['item99999']).to eq('value99999')
      expect(hot['east']['item0']).to be_nil
      expect(hot.line_of('north', 'item99999')).to eq(count + 5)
      expect(hot.line_of('east', 'item0')).to be_nil
      elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started

      # the rename plus these key lookups cost proportionally to the
      # edit and the queries, not to the hundred thousand untouched rules
      expect(elapsed).to be < 1.0
      expect(hot.get_groups).to eq(%w[west north east])
    end
  end

  describe 'equivalence with a full ParseConfig read (randomized)' do
    NAMES = %w[alpha beta gamma].freeze
    KEYS = %w[route cursor host].freeze

    def random_token(rng)
      case rng.rand(12)
      when 0..3 then "#{KEYS.sample(random: rng)}: v#{rng.rand(20)}"
      when 4 then "#{KEYS.sample(random: rng)}: \"q#{rng.rand(5)}\""
      when 5..6 then "[#{NAMES.sample(random: rng)}]"
      when 7 then "[#{NAMES.sample(random: rng)}] ! note"
      when 8 then NAMES.sample(random: rng)
      when 9 then KEYS.sample(random: rng)
      when 10 then '! remark'
      else ''
      end
    end

    def random_edits(rng, line_count)
      edits = []
      touched = {}
      rng.rand(1..4).times do
        case rng.rand(3)
        when 0
          edits << { op: :insert, before: rng.rand(1..line_count + 1), text: random_token(rng) }
        when 1
          line = rng.rand(1..line_count)
          next if touched[line]

          touched[line] = true
          edits << { op: :delete, line: line }
        when 2
          line = rng.rand(1..line_count)
          next if touched[line]

          touched[line] = true
          edits << { op: :replace, line: line, text: random_token(rng) }
        end
      end
      edits
    end

    def apply_to_lines(lines, edits)
      deletes = {}
      replaces = {}
      inserts = Hash.new { |hash, key| hash[key] = [] }
      edits.each do |edit|
        case edit[:op]
        when :delete then deletes[edit[:line]] = true
        when :replace then replaces[edit[:line]] = edit[:text]
        when :insert then inserts[edit[:before]] << edit[:text]
        end
      end

      result = []
      lines.each_with_index do |line, index|
        lineno = index + 1
        result.concat(inserts[lineno])
        next if deletes[lineno]

        result << (replaces.key?(lineno) ? replaces[lineno] : line)
      end
      result.concat(inserts[lines.size + 1])
      result
    end

    it 'matches values, types, group order and line numbers after every batch' do
      rng = Random.new(20_261_128)
      40.times do
        lines = Array.new(rng.rand(5..15)) { random_token(rng) }
        begin
          config = fresh_parse(lines)
        rescue ArgumentError, NoMethodError
          next # the initial document itself must be readable
        end

        hot = new_hot(lines)
        expect_equivalent(hot, config, lines)

        3.times do
          edits = random_edits(rng, lines.length)
          candidate = apply_to_lines(lines, edits)
          begin
            config = fresh_parse(candidate)
          rescue ArgumentError, NoMethodError
            # the candidate is unreadable: the whole batch must be rejected
            expect { hot.publish(edits) }.to raise_error(ArgumentError)
            expect_equivalent(hot, fresh_parse(lines), lines)
            next
          end

          hot.publish(edits)
          lines = candidate
          expect_equivalent(hot, config, lines)
        end
      end
    end
  end
end
