# lifecycle-manager/Dockerfile
# OpenSpace Lifecycle Manager - Complete installer with onboarder wrapped

# =============================================================================
# Base Image - Onboarder
# =============================================================================
ARG ONBOARDER_VERSION
FROM onboarder-full:${ONBOARDER_VERSION}

# =============================================================================
# Build Arguments
# =============================================================================
ARG ONBOARDER_VERSION
ARG LIFECYCLE_VERSION

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

# Create openspace structure
# Note: Onboarder already has /docker-workspace
RUN mkdir -p /openspace/data

# Copy top-level Taskfile
COPY data/taskfiles/Taskfile.yml /openspace/Taskfile.yml

# Copy all orchestration data (playbooks, scripts, templates, taskfiles)
COPY data/ /openspace/data/

# =============================================================================
# Environment Setup
# =============================================================================

# Set working directory
WORKDIR /openspace

# Onboarder Environment variables
ENV ONBOARDER_WORK_DIR=/docker-workspace \
    ONBOARDER_CONFIG_DIR=/docker-workspace/config \
    ONBOARDER_COMMON_DIR=/docker-workspace/config/common \
    ONBOARDER_SAMPLE_VSPHERE_DIR=/docker-workspace/config/sample-vsphere \
    ONBOARDER_INSTALL_DIR=/docker-workspace/config/install \
    ONBOARDER_CACHE_DIR=/docker-workspace/config/install/.cache
    ONBOARDER_VERSION=${ONBOARDER_VERSION}

# Lifecycle Manager Environment variables
ENV OLM_WORK_DIR=/openspace \
    OLM_DATA_DIR=/openspace/data \
    OLM_LIFECYCLE_VERSION=${LIFECYCLE_VERSION}

RUN echo '' >> /root/.bashrc && \
    echo '# Lifecycle Manager Prompt' >> /root/.bashrc && \
    echo 'export PS1="\[\e[32m\]lifecycle_mgr\[\e[34m\]:${OLM_LIFECYCLE_VERSION} \[\e[37m\]\w\[\e[0m\] # "' >> /root/.bashrc

RUN echo 'alias ls="ls --color=auto"' >> /root/.bashrc && \
    echo 'export LS_COLORS=${LS_COLORS:-"di=34:ln=36:so=35:pi=33:ex=32"}' >> /root/.bashrc


# =============================================================================
# Default Command
# =============================================================================

CMD ["/bin/bash"]