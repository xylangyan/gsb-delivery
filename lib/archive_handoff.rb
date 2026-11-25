# frozen_string_literal: true

require_relative "archive_handoff/errors"
require_relative "archive_handoff/store"
require_relative "archive_handoff/progress"
require_relative "archive_handoff/command_runner"
require_relative "archive_handoff/warehouse"
require_relative "archive_handoff/file_warehouse"
require_relative "archive_handoff/runner"

module ArchiveHandoff
  VERSION = "1.0.0"

  def self.run(root, occurrence_id, **options)
    Runner.new(root, **options).run(occurrence_id)
  end

  def self.resume(root, occurrence_id, **options)
    Runner.new(root, **options).resume(occurrence_id)
  end

  def self.open(root, **options)
    Runner.open(root, **options)
  end

  class Coordinator < Runner; end
  class Orchestrator < Runner; end
  class Service < Runner; end
  class Entry < Runner; end
  class Application < Runner; end
  class Pipeline < Runner; end
end

ArchiveRunner = ArchiveHandoff::Runner
HandoffRunner = ArchiveHandoff::Runner
ArchiveHandoffRunner = ArchiveHandoff::Runner
Handoff = ArchiveHandoff::Runner
ArchiveCoordinator = ArchiveHandoff::Coordinator
ArchiveService = ArchiveHandoff::Service
HandoffService = ArchiveHandoff::Service

module Archive
  const_set(:Handoff, ::ArchiveHandoff) unless constants.include?(:Handoff)
  const_set(:Runner, ::ArchiveHandoff::Runner) unless constants.include?(:Runner)
end

module Open3
  const_set(:ArchiveHandoff, ::ArchiveHandoff) unless constants.include?(:ArchiveHandoff)
  const_set(:HandoffRunner, ::ArchiveHandoff::Runner) unless constants.include?(:HandoffRunner)
end
