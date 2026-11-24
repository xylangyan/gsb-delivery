module Kiba
  # Opt-in building blocks for jobs which want to survive a stop and continue
  # later without replaying work already acknowledged by their destinations.
  #
  # A resumable job is run with Kiba::ResumableRunner. Sources which take part
  # expose pages and cursors, transforms may expose a replayable state and
  # destinations may recognise their own write identifiers. Components which
  # do not implement these methods keep working, but only components which do
  # can provide the corresponding guarantee.
  module Resumable
    # A source may include this module for documentation, but duck typing is
    # what the runner relies on: implement #read_page(cursor) (or #next_page)
    # and return a Page (or [rows, next_cursor], or a hash with :rows and
    # :next_cursor).
    module Source
    end

    # A transform may implement #snapshot and #restore(state). Snapshot must
    # return a Marshal-able value. Restoring it must make the following
    # #process / #close calls produce the same sequence again.
    module Transform
    end

    # A destination may implement #write(row, write_id: id) and
    # #landed?(write_id). When landing cannot be queried, writes are still
    # attempted exactly once per acknowledged checkpoint.
    module Destination
    end

    Page = Kiba::Checkpoint::Page

    module_function

    def run(job = nil, checkpoint: nil, &block)
      job ||= Kiba.parse(&block) if block
      fail ArgumentError, "a job or a block is required" if job.nil?
      ResumableRunner.run(job, checkpoint: checkpoint)
    end

    def status(job = nil, checkpoint: nil, &block)
      job ||= Kiba.parse(&block) if block
      fail ArgumentError, "a job or a block is required" if job.nil?
      ResumableRunner.status(job, checkpoint: checkpoint)
    end
  end
end
