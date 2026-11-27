# frozen_string_literal: true

# Usage: ruby spec/benchmarks/profiling.rb && open tmp/stack.html

require 'ruby-prof'

require_relative "../../lib/batch_loader"
require_relative "../fixtures/models"

User.save(id: 1)
iterations = Array.new(5_000)

def batch_loader
  BatchLoader.for(1).batch do |ids, loader|
    User.where(id: ids).each { |user| loader.call(user.id, user) }
  end
end

RubyProf.measure_mode = RubyProf::WALL_TIME
RubyProf.start

iterations.each { batch_loader.id } # 2.46, 2.87, 2.56 sec

result = RubyProf.stop
stack_printer = RubyProf::CallStackPrinter.new(result)
File.open("tmp/stack.html", 'w') { |file| stack_printer.print(file) }
