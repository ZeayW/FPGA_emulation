FROM mcr.microsoft.com/devcontainers/cpp:1-ubuntu-24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bison \
        flex \
        jq \
        libboost-serialization-dev \
        libboost-system-dev \
        libboost-thread-dev \
        libeigen3-dev \
        liblemon-dev \
        libomp-dev \
        libspdlog-dev \
        ninja-build \
        python3-pip \
        python3-venv \
        swig \
        tcl-dev \
        time \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
