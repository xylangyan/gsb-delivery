# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe UriJoin do
      describe ".resolve" do
        {
          ["http://a/b/c/d;p?q", "g"] => "http://a/b/c/g",
          ["http://a/b/c/d;p?q", "./g"] => "http://a/b/c/g",
          ["http://a/b/c/d;p?q", "/g"] => "http://a/g",
          ["http://a/b/c/d;p?q", "//g"] => "http://g",
          ["http://a/b/c/d;p?q", "?y"] => "http://a/b/c/d;p?y",
          ["http://a/b/c/d;p?q", "#s"] => "http://a/b/c/d;p?q#s",
          ["http://a/b/c/d;p?q", ""] => "http://a/b/c/d;p?q",
          ["http://a/b/c/d;p?q", "../../../g"] => "http://a/g",
          ["http://a/b/c/d;p?q", "g/../h"] => "http://a/b/c/h"
        }.each do |(base, reference), expected|
          it "resolves #{reference.inspect} against #{base.inspect}" do
            expect(described_class.resolve(base, reference)).to eq(expected)
          end
        end

        it "returns the reference unchanged when the base is missing" do
          expect(described_class.resolve(nil, "x.mp3")).to eq("x.mp3")
        end

        it "returns an absolute reference unchanged" do
          expect(described_class.resolve("https://x/", "https://y/z")).to eq("https://y/z")
        end

        it "returns the reference when the pair cannot be resolved" do
          expect(described_class.resolve("http://[bad", "x")).to eq("x")
        end
      end

      describe ".resolve_chain" do
        it "applies the bases outermost first" do
          expect(described_class.resolve_chain(["https://x/y/", "sub/"], "a.mp3"))
            .to eq("https://x/y/sub/a.mp3")
        end

        it "ignores blank bases" do
          expect(described_class.resolve_chain([nil, "", "https://x/"], "a"))
            .to eq("https://x/a")
        end

        it "returns the reference when no base is present" do
          expect(described_class.resolve_chain([], "a")).to eq("a")
        end
      end

      describe ".blank?" do
        it "treats whitespace and nil as blank" do
          expect(described_class.blank?(nil)).to be(true)
          expect(described_class.blank?("  ")).to be(true)
          expect(described_class.blank?("x")).to be(false)
        end
      end
    end
  end
end
