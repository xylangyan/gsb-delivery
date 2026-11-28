module Kiba
  # Tracks whether a row currently being delivered came from a transform's
  # ordinary process flow or from its close flow.
  #
  # This is opt-in and only used by the fan-out interface. Ordinary Kiba jobs
  # keep their existing behavior; the thread-local is set and restored around
  # each yielded row without changing what is delivered.
  module RowPhase
    WRITE = :write
    CLOSE = :close
    KEY = :kiba_row_phase

    module_function

    def current
      Thread.current[KEY] || WRITE
    end

    def close?
      current == CLOSE
    end

    # Close origin is sticky: once a row came from a close flow, downstream
    # transforms processing it must not relabel it as an ordinary write.
    def with_write
      previous = Thread.current[KEY]
      Thread.current[KEY] = WRITE if previous.nil?
      yield
    ensure
      Thread.current[KEY] = previous
    end

    def with_close
      previous = Thread.current[KEY]
      Thread.current[KEY] = CLOSE
      yield
    ensure
      Thread.current[KEY] = previous
    end
  end
end
