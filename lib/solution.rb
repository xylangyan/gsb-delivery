# frozen_string_literal: true

require_relative 'fugit'

Solution = Fugit::PhasePlanner unless Object.const_defined?(:Solution)
