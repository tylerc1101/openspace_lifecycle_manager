# lifecycle-manager/scripts/download-onboarder.sh
#!/bin/bash
set -e

#==============================================================================
# Configuration
#==============================================================================

ARTIFACTORY_URL="${ARTIFACTORY_URL:-https://bits.devops.kratosdefense.com}"
ARTIFACTORY_REPO="${ARTIFACTORY_REPO:-kratos-openspace-packages-staging-local/openspace-rancher-onboarder}"
ONBOARDER_VERSION="${ONBOARDER_VERSION:-3.5.0-rc7}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-onboarder-image}"

# Filename pattern: onboarder-full.v{VERSION}.tar.gz.{aa|ab}
ONBOARDER_BASE_NAME="onboarder-full.v${ONBOARDER_VERSION}"
ONBOARDER_TARBALL="${DOWNLOAD_DIR}/onboarder-${ONBOARDER_VERSION}.tar.gz"

#==============================================================================
# Colors
#==============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

#==============================================================================
# Functions
#==============================================================================

log_info() { echo -e "${BLUE}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1" >&2; }
log_warning() { echo -e "${YELLOW}⚠${NC} $1"; }

# Show download progress by monitoring file size
show_progress() {
    local file="$1"
    local label="$2"

    while [ ! -f "$file" ]; do
        sleep 0.5
    done

    while kill -0 $! 2>/dev/null; do
        if [ -f "$file" ]; then
            local size=$(du -h "$file" 2>/dev/null | cut -f1)
            printf "\r    %s: %s downloaded..." "$label" "$size"
        fi
        sleep 1
    done

    local final_size=$(du -h "$file" 2>/dev/null | cut -f1)
    printf "\r    %s: %s downloaded    \n" "$label" "$final_size"
}

#==============================================================================
# Get Credentials
#==============================================================================

get_credentials() {
    if [ -n "$ARTIFACTORY_USERNAME" ] && [ -n "$ARTIFACTORY_PASSWORD" ]; then
        log_info "Using credentials from environment"
        return 0
    fi

    echo ""
    read -p "Artifactory Username: " ARTIFACTORY_USERNAME
    read -sp "Artifactory Password: " ARTIFACTORY_PASSWORD
    echo ""
    echo ""
}

#==============================================================================
# Download Files
#==============================================================================

download_parts() {
    log_info "Downloading onboarder parts..."
    echo ""

    local part1_url="${ARTIFACTORY_URL}/artifactory/${ARTIFACTORY_REPO}/${ONBOARDER_VERSION}/${ONBOARDER_BASE_NAME}.tar.gz.aa"
    local part2_url="${ARTIFACTORY_URL}/artifactory/${ARTIFACTORY_REPO}/${ONBOARDER_VERSION}/${ONBOARDER_BASE_NAME}.tar.gz.ab"
    local part1_file="${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.aa"
    local part2_file="${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.ab"

    # Download part 1
    echo -e "  ${BLUE}→${NC} Downloading part 1/2 (aa)..."

    curl -f -u "${ARTIFACTORY_USERNAME}:${ARTIFACTORY_PASSWORD}" \
        --silent \
        --output "${part1_file}" \
        "${part1_url}" &

    show_progress "$part1_file" "Part 1"
    wait $!

    if [ $? -eq 0 ]; then
        echo -e "    ${GREEN}✓${NC} Part 1 complete"
    else
        echo -e "    ${RED}✗${NC} Failed to download part 1"
        exit 1
    fi

    # Download part 2
    echo ""
    echo -e "  ${BLUE}→${NC} Downloading part 2/2 (ab)..."

    curl -f -u "${ARTIFACTORY_USERNAME}:${ARTIFACTORY_PASSWORD}" \
        --silent \
        --output "${part2_file}" \
        "${part2_url}" &

    show_progress "$part2_file" "Part 2"
    wait $!

    if [ $? -eq 0 ]; then
        echo -e "    ${GREEN}✓${NC} Part 2 complete"
    else
        echo -e "    ${RED}✗${NC} Failed to download part 2"
        exit 1
    fi
}

#==============================================================================
# Combine Parts
#==============================================================================

combine_parts() {
    echo ""
    log_info "Combining split files..."

    cat "${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.aa" \
        "${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.ab" \
        > "${ONBOARDER_TARBALL}"

    echo -e "  ${GREEN}✓${NC} Combined into: ${ONBOARDER_TARBALL}"

    # Remove split files
    rm -f "${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.aa"
    rm -f "${DOWNLOAD_DIR}/${ONBOARDER_BASE_NAME}.tar.gz.ab"

    echo -e "  ${BLUE}→${NC} Removed split files"
}

#==============================================================================
# Verify Tarball
#==============================================================================

verify_tarball() {
    echo ""
    log_info "Verifying tarball integrity..."

    if tar tzf "${ONBOARDER_TARBALL}" >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} Tarball is valid"
    else
        echo -e "  ${RED}✗${NC} Tarball is corrupted"
        exit 1
    fi
}

#==============================================================================
# Main
#==============================================================================

main() {
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║  Downloading Onboarder from Artifactory                       ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "Version: ${ONBOARDER_VERSION}"
    echo "Source: ${ARTIFACTORY_URL}"
    echo "Files: ${ONBOARDER_BASE_NAME}.tar.gz.{aa,ab}"
    echo ""

    # Create download directory
    mkdir -p "${DOWNLOAD_DIR}"

    # Get credentials
    get_credentials

    # Download parts
    download_parts

    # Combine parts
    combine_parts

    # Verify tarball
    verify_tarball

    # Success
    echo ""
    log_success "Onboarder download complete"
    echo ""
    echo "Tarball: ${ONBOARDER_TARBALL}"
    echo "Size: $(du -h ${ONBOARDER_TARBALL} | cut -f1)"
    echo ""
}

# Run
main "$@"