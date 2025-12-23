# lifecycle-manager/Dockerfile
# OpenSpace Lifecycle Manager - Complete installer with onboarder wrapped

# =============================================================================
# Build Arguments
# =============================================================================
ARG ONBOARDER_VERSION=3.5.0-rc7
ARG LIFECYCLE_VERSION=1.1.0

# =============================================================================
# Base Image - Onboarder
# =============================================================================
FROM onboarder:${ONBOARDER_VERSION}

# =============================================================================
# Metadata
# =============================================================================
LABEL org.opencontainers.image.title="OpenSpace Lifecycle Manager"
LABEL org.opencontainers.image.description="Complete installer for OpenSpace Platform with wrapped onboarder"
LABEL org.opencontainers.image.version="${LIFECYCLE_VERSION}"
LABEL org.opencontainers.image.vendor="Kratos"
LABEL org.opencontainers.image.authors="David Camp"
LABEL org.opencontainers.image.source="https://github.com/yourorg/openspace-platform"
LABEL org.opencontainers.image.documentation="https://docs.yourorg.com/openspace"

# Component versions
LABEL component.onboarder.version="${ONBOARDER_VERSION}"
LABEL component.orchestration.version="${LIFECYCLE_VERSION}"

# =============================================================================
# Install Orchestration Dependencies
# =============================================================================

# System packages
RUN dnf install -y \
    sshpass

# Taskfile installation
ARG TASKFILE_VERSION=3.39.2
RUN curl -sL "https://github.com/go-task/task/releases/download/v${TASKFILE_VERSION}/task_linux_amd64.tar.gz" | \
    tar xz -C /usr/local/bin task && \
    chmod +x /usr/local/bin/task && \
    task --version

# =============================================================================
# Setup Orchestration Workspace
# =============================================================================

# Create lifecycle_manager structure
# Note: Onboarder already has /docker-workspace
RUN mkdir -p /lifecycle_manager/data

# Copy top-level Taskfile
COPY data/taskfiles/Taskfile.yml /lifecycle_manager/Taskfile.yml

# Copy all orchestration data (playbooks, scripts, templates, taskfiles)
COPY data/ /lifecycle_manager/data/

# =============================================================================
# Environment Setup
# =============================================================================

# Set working directory
WORKDIR /lifecycle_manager

# Environment variables
ENV ONBOARDER_DIR=/docker-workspace \
    ORCHESTRATION_DIR=/lifecycle_manager \
    DATA_DIR=/lifecycle_manager/data \
    LIFECYCLE_VERSION=${LIFECYCLE_VERSION} \
    ONBOARDER_VERSION=${ONBOARDER_VERSION}

# =============================================================================
# Default Command
# =============================================================================

CMD ["/bin/bash"]