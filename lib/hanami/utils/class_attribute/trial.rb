# frozen_string_literal: true

require "concurrent/map"

module Hanami
  module Utils
    module ClassAttribute
      # Trial run ("preview") window for class attributes.
      #
      # A trial window isolates a set of class attribute reads and writes from
      # the values seen outside the window ("production"). Inside the window the
      # usual getters, setters and Ruby Array/Hash operations can be used to
      # preview changes to the configuration. When the window ends, production
      # values are left untouched.
      #
      # Windows are local to the current fiber: another fiber of the same
      # thread opens its own independent window, and readers outside any window
      # keep reading the production values.
      #
      # Windows can be nested. A nested window previews on top of the enclosing
      # one: when it ends normally its changes are merged into the enclosing
      # window, when it raises its changes are rolled back.
      #
      # Mutable containers assigned to attributes while a window is open are
      # expected to come from the window itself or to be newly created inside
      # it.
      #
      # @since 3.0.1
      # @api private
      #
      # @example
      #   Hanami::Utils::ClassAttribute.trial do
      #     North.routes << {match: :oversize, target: :oversize}
      #     North.routes.first[:target] = :trial
      #   end
      #   North.routes # => production value, unchanged
      class Trial
        # @since 3.0.1
        # @api private
        STACK_KEY = :__hanami_utils_class_attribute_trial__

        class << self
          # The innermost window open on the current fiber, if any.
          #
          # @since 3.0.1
          # @api private
          def current
            stack.last
          end

          # Opens a trial window for the duration of the block.
          #
          # @since 3.0.1
          # @api private
          def call
            frame = Frame.new(current)
            stack.push(frame)

            begin
              result = yield
            rescue Exception # rubocop:disable Lint/RescueException
              frame.rollback
              raise
            else
              frame.commit
              result
            ensure
              stack.pop
            end
          end

          private

          # @since 3.0.1
          # @api private
          def stack
            Thread.current[STACK_KEY] ||= []
          end
        end

        # Deep copy/restore for configurations made of arrays, hashes and
        # scalars. Copies preserve the sharing topology of the source via a
        # memo, so objects shared between attributes stay shared inside the
        # window. Restore mutates the given containers in place, so references
        # held elsewhere observe the restored contents.
        #
        # @since 3.0.1
        # @api private
        module DeepCopy
          module_function

          # @since 3.0.1
          # @api private
          def copy(value, memo)
            case value
            when ::Array
              return memo[value.object_id] if memo.key?(value.object_id)

              result = []
              memo[value.object_id] = result
              value.each { |element| result << copy(element, memo) }
              result
            when ::Hash
              return memo[value.object_id] if memo.key?(value.object_id)

              result = {}
              memo[value.object_id] = result
              value.each { |key, element| result[copy(key, memo)] = copy(element, memo) }
              result
            else
              value
            end
          end

          # @since 3.0.1
          # @api private
          def restore(current, snapshot)
            case snapshot
            when ::Array
              return copy(snapshot, {}) unless current.is_a?(::Array)

              current.pop while current.length > snapshot.length
              snapshot.each_with_index do |element, index|
                current[index] = restore(current[index], element)
              end
              current
            when ::Hash
              return copy(snapshot, {}) unless current.is_a?(::Hash)

              current.delete_if { |key, _| !snapshot.key?(key) }
              snapshot.each do |key, element|
                current[key] = restore(current[key], element)
              end
              current
            else
              snapshot
            end
          end
        end

        # A single trial window. The root window deep copies production values
        # on first access; nested windows shallow copy the enclosing window's
        # values (keeping inner shared objects shared) and snapshot them for
        # rollback.
        #
        # @since 3.0.1
        # @api private
        class Frame
          # @since 3.0.1
          # @api private
          def initialize(parent)
            @parent = parent
            @entries = {}
            @snapshots = {}
            @memo = {}
          end

          # @since 3.0.1
          # @api private
          def fetch(klass)
            @entries[klass] ||= materialize(klass)
          end

          # Registers the attributes of a class created inside the window, so
          # it keeps sharing inner objects with the rest of the window.
          #
          # @since 3.0.1
          # @api private
          def register(klass, attributes)
            @entries[klass] = attributes
          end

          # Merges this window's changes into the enclosing one. A root window
          # has nowhere to merge into: its changes are discarded, leaving
          # production untouched.
          #
          # @since 3.0.1
          # @api private
          def commit
            return if root?

            @entries.each do |klass, attributes|
              target = @parent.fetch(klass)
              attributes.each do |key, value|
                target[key] = value
              end
            end
          end

          # Restores, in place, the enclosing window's objects mutated through
          # this window.
          #
          # @since 3.0.1
          # @api private
          def rollback
            return if root?

            @snapshots.each do |klass, snapshot|
              target = @parent.fetch(klass)
              snapshot.each do |key, value|
                target[key] = DeepCopy.restore(target[key], value)
              end
            end
          end

          private

          # @since 3.0.1
          # @api private
          def root?
            @parent.nil?
          end

          # @since 3.0.1
          # @api private
          def materialize(klass)
            source = if root?
                       klass.instance_variable_get(:@__class_attributes)
                     else
                       @parent.fetch(klass)
                     end

            if root?
              deep_copy_attributes(source)
            else
              @snapshots[klass] = snapshot_attributes(source)
              source.dup
            end
          end

          # @since 3.0.1
          # @api private
          def deep_copy_attributes(source)
            attributes = Concurrent::Map.new
            source.each do |key, value|
              attributes[key.to_sym] = DeepCopy.copy(value, @memo)
            end
            Attributes.new(attributes: attributes)
          end

          # @since 3.0.1
          # @api private
          def snapshot_attributes(source)
            memo = {}
            {}.tap do |snapshot|
              source.each do |key, value|
                snapshot[key] = DeepCopy.copy(value, memo)
              end
            end
          end
        end
      end

      # Opens a trial run window for class attributes.
      #
      # @return [Object] the block's return value
      #
      # @since 3.0.1
      #
      # @see Hanami::Utils::ClassAttribute::Trial
      #
      # @example
      #   Hanami::Utils::ClassAttribute.trial do
      #     North.routes << {match: :oversize, target: :oversize}
      #   end
      def self.trial(&block)
        Trial.call(&block)
      end
    end
  end
end
