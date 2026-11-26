module Dotenv
  # A `.env` file that will be read and parsed into a Hash
  class Environment < Hash
    attr_reader :filename, :overwrite

    # Create a new Environment
    #
    # @param filename [String] the path to the file to read
    # @param overwrite [Boolean] whether the parser should assume existing values will be overwritten
    # @param env [#[], #key?] environment consulted for existing values and substitutions
    # @param substitutions [Array] substitution passes to run over parsed values
    def initialize(filename, overwrite: false, env: ENV, substitutions: nil)
      super()
      @filename = filename
      @overwrite = overwrite
      @env = env
      @substitutions = substitutions
      load
    end

    def load
      update Parser.call(read, overwrite: overwrite, env: @env, substitutions: @substitutions)
    rescue FormatError => e
      e.filename ||= @filename
      raise
    end

    def read
      File.open(@filename, "rb:bom|utf-8", &:read)
    end
  end
end
