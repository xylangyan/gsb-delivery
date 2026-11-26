# frozen_string_literal: true

require 'json'

module Fluent
  module Logger
    module ReceiptAnalysis
      # Command line front end. Everything prints JSON so the output can be
      # consumed by a checker.
      #
      #   receipt-analysis certify ARCHIVE --range MIN..MAX [--pilot L,...]
      #   receipt-analysis run ARCHIVE --limit L [--format json|text]
      #   receipt-analysis query ARCHIVE --limit L --checkpoint NAME [--number ID]
      class CLI
        def self.run(argv)
          new.run(argv)
        end

        def run(argv)
          command = argv.shift
          case command
          when 'certify' then certify(argv)
          when 'run' then run_limit(argv)
          when 'query' then query(argv)
          when 'help', '-h', '--help', nil then help
          else
            warn "unknown command: #{command}"
            help
            64
          end
        end

        private

        def parse(argv)
          options = {}
          args = []
          until argv.empty?
            token = argv.shift
            case token
            when '--limit', '-l' then options[:limit] = Integer(argv.shift)
            when '--range', '-r'
              range = argv.shift
              from, to = range.split('..', 2)
              options[:range] = (Integer(from)..Integer(to))
            when '--pilot'
              options[:pilot] = argv.shift.split(',').map { |value| Integer(value) }
            when '--threshold'
              options[:threshold] = argv.shift.split(',').map { |value| Integer(value) }
            when '--checkpoint', '-c' then options[:checkpoint] = argv.shift
            when '--number', '-n' then options[:number] = argv.shift
            when '--format', '-f' then options[:format] = argv.shift
            else args << token
            end
          end
          [args, options]
        end

        def certify(argv)
          args, options = parse(argv)
          path = args.first or return usage('certify ARCHIVE --range MIN..MAX')
          range = options[:range] or return usage('certify requires --range MIN..MAX')
          archive = Archive.from_json_file(path)
          analysis = Analysis.new(archive)
          certification = analysis.certify_intervals(
            range,
            pilot_limits: options[:pilot],
            thresholds: options[:threshold]
          )
          puts JSON.pretty_generate(
            'archive' => path,
            'range' => [range.begin, range.end],
            'intervals' => certification.to_a.map { |interval| [interval.begin, interval.end] }
          )
          0
        end

        def run_limit(argv)
          args, options = parse(argv)
          path = args.first or return usage('run ARCHIVE --limit L')
          limit = options[:limit]
          unless limit
            # Without --limit, run every limit in --range (short archives only).
            range = options[:range] or return usage('run requires --limit or --range')
            results = range.map { |value| Analysis.new(Archive.from_json_file(path)).run(value) }
            print_results(results, options)
            return 0
          end
          archive = Archive.from_json_file(path)
          result = Analysis.new(archive).run(limit)
          print_result(result, options)
          0
        end

        def query(argv)
          args, options = parse(argv)
          path = args.first or return usage('query ARCHIVE --limit L --checkpoint NAME [--number ID]')
          limit = options[:limit] or return usage('query requires --limit L')
          checkpoint = options[:checkpoint] or return usage('query requires --checkpoint NAME')
          archive = Archive.from_json_file(path)
          analysis = Analysis.new(archive)
          result = analysis.run(limit)
          state = result.checkpoint_state(checkpoint)
          if state.nil?
            warn "checkpoint #{checkpoint} was not reached"
            return 65
          end
          payload = { 'limit' => limit, 'checkpoint' => checkpoint }
          if options[:number]
            number = options[:number]
            payload['number'] = number
            payload['complete_count'] = state.count(number)
            payload['receipts'] = state.occurrences(number).map(&:to_h)
            payload['callback'] = state.callback_occurrences(number).map(&:to_h)
            payload['pending'] = state.pending_numbers.include?(number)
          else
            payload.merge!(state.to_h)
          end
          puts JSON.pretty_generate(payload)
          0
        end

        def print_result(result, options)
          if options[:format] == 'text'
            puts "limit=#{result.limit}"
            result.totals.sort.each { |number, count| puts "  #{number}: #{count}" unless count.zero? }
            puts "  callback_bytes: #{result.callback_bytes}"
            puts "  pending_bytes: #{result.final_pending.sum(&:bytesize)}"
            puts "  pending: #{result.final_pending.map(&:number).inspect}"
          else
            puts JSON.pretty_generate(result.to_h)
          end
        end

        def print_results(results, options)
          if options[:format] == 'text'
            results.each { |result| print_result(result, options) }
          else
            puts JSON.pretty_generate(results.map(&:to_h))
          end
        end

        def usage(text)
          warn "usage: receipt-analysis #{text}"
          64
        end

        def help
          puts <<~HELP
            receipt-analysis — buffer_limit receipt certification

            Commands:
              certify ARCHIVE --range MIN..MAX [--pilot L,...] [--threshold C,...]
                  Print the merged intervals of buffer_limit values whose receipt
                  passes every checkpoint.

              run ARCHIVE --limit L [--format json|text]
              run ARCHIVE --range MIN..MAX [--format json|text]
                  Replay one limit (or every limit) and print receipts, overflow
                  bytes, and what is still pending.

              query ARCHIVE --limit L --checkpoint NAME [--number ID]
                  Report how many times an original number was received, from
                  which connections and submissions, or what bytes reached the
                  overflow callback / are still pending.
          HELP
          0
        end
      end
    end
  end
end
