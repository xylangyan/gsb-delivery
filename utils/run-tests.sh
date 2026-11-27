#!/bin/bash

pushd tests
    rspec test_parseconfig.rb test_hot_parseconfig.rb
popd
