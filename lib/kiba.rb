require "kiba/version"

require "kiba/control"
require "kiba/context"
require "kiba/parser"
require "kiba/streaming_runner"
require "kiba/checkpoint"
require "kiba/resumable_runner"
require "kiba/resumable"
require "kiba/dsl_extensions/config"

Kiba.extend(Kiba::Parser)

module Kiba
  def self.run(job = nil, checkpoint: nil, &block)
    unless job.nil? ^ block.nil?
      fail ArgumentError.new("Kiba.run takes either one argument (the job) or a block (defining the job)")
    end

    job ||= Kiba.parse { instance_exec(&block) }

    runner = runner_for(job, checkpoint)
    if runner == Kiba::ResumableRunner && !checkpoint.nil?
      runner.run(job, checkpoint: checkpoint)
    else
      runner.run(job)
    end
  end

  def self.resume(job = nil, checkpoint: nil, &block)
    job ||= Kiba.parse(&block) if block
    fail ArgumentError, "a job or a block is required" if job.nil?
    Kiba::ResumableRunner.run(job, checkpoint: checkpoint)
  end

  def self.status(job = nil, checkpoint: nil, &block)
    job ||= Kiba.parse(&block) if block
    fail ArgumentError, "a job or a block is required" if job.nil?
    Kiba::ResumableRunner.status(job, checkpoint: checkpoint)
  end

  def self.runner_for(job, checkpoint = nil)
    # Supplying a checkpoint is itself a request for resumability, and takes
    # precedence over the default runner.
    return Kiba::ResumableRunner unless checkpoint.nil?

    config = job.config.fetch(:kiba, {})
    configured = config[:runner]
    return configured if configured
    return Kiba::ResumableRunner if config[:resumable] || config[:checkpoint] || config[:checkpoint_store]
    Kiba::StreamingRunner
  end
end
