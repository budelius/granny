#!/bin/bash
#
# Cloudflare DNS Record Manager
# Manages DNS A records for pseekoo.io and pseekoo.com subdomains
#
# Usage:
#   ./manage-dns.sh add <subdomain>              # Add/update A record with auto-detected IP (pseekoo.io)
#   ./manage-dns.sh add <subdomain> <ip>         # Add/update A record with specific IP
#   ./manage-dns.sh add <subdomain> --domain <domain>  # Use specific domain
#   ./manage-dns.sh delete <subdomain>           # Delete A record
#   ./manage-dns.sh list [--domain <domain>]     # List all DNS records
#   ./manage-dns.sh get <subdomain>              # Get specific record details
#
# Environment:
#   DOMAIN=pseekoo.com ./manage-dns.sh add foo   # Alternative way to set domain
#
# Examples:
#   ./manage-dns.sh add qdrant                   # Creates qdrant.pseekoo.io
#   ./manage-dns.sh add api 1.2.3.4              # Creates api.pseekoo.io with specific IP
#   ./manage-dns.sh add holylist --domain pseekoo.com  # Creates holylist.pseekoo.com
#   ./manage-dns.sh delete qdrant                # Deletes qdrant.pseekoo.io
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DOMAIN="${DOMAIN:-pseekoo.io}"  # Default domain, can be overridden via env or --domain flag

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Load environment
if [[ -f "$ENV_FILE" ]]; then
    source "$ENV_FILE"
else
    echo -e "${RED}Error: .env file not found at $ENV_FILE${NC}"
    exit 1
fi

if [[ -z "$CLOUDFLARE_API_TOKEN" ]]; then
    echo -e "${RED}Error: CLOUDFLARE_API_TOKEN not set in .env${NC}"
    exit 1
fi

CF_API="https://api.cloudflare.com/client/v4"

# Helper function for API calls
cf_api() {
    local method="$1"
    local endpoint="$2"
    local data="$3"

    if [[ -n "$data" ]]; then
        curl -s -X "$method" "$CF_API$endpoint" \
            -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
            -H "Content-Type: application/json" \
            --data "$data"
    else
        curl -s -X "$method" "$CF_API$endpoint" \
            -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
            -H "Content-Type: application/json"
    fi
}

# Get zone ID for domain
get_zone_id() {
    local response
    response=$(cf_api GET "/zones?name=$DOMAIN")

    if ! echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        echo -e "${RED}Error: Failed to query Cloudflare API${NC}"
        echo "$response" | jq . 2>/dev/null || echo "$response"
        exit 1
    fi

    local zone_id
    zone_id=$(echo "$response" | jq -r '.result[0].id')

    if [[ "$zone_id" == "null" || -z "$zone_id" ]]; then
        echo -e "${RED}Error: Zone '$DOMAIN' not found${NC}"
        exit 1
    fi

    echo "$zone_id"
}

# Get public IP of this server
get_public_ip() {
    local ip
    ip=$(curl -s https://api.ipify.org 2>/dev/null || \
         curl -s https://ifconfig.me 2>/dev/null || \
         curl -s https://icanhazip.com 2>/dev/null)

    if [[ -z "$ip" ]]; then
        echo -e "${RED}Error: Could not detect public IP${NC}"
        exit 1
    fi

    echo "$ip"
}

# Get record ID by name
get_record_id() {
    local zone_id="$1"
    local name="$2"
    local full_name="${name}.${DOMAIN}"

    local response
    response=$(cf_api GET "/zones/$zone_id/dns_records?type=A&name=$full_name")

    echo "$response" | jq -r '.result[0].id // empty'
}

# Add or update DNS record
cmd_add() {
    local subdomain="$1"
    local ip="$2"

    if [[ -z "$subdomain" ]]; then
        echo -e "${RED}Error: Subdomain required${NC}"
        echo "Usage: $0 add <subdomain> [ip]"
        exit 1
    fi

    if [[ -z "$ip" ]]; then
        echo -e "${BLUE}Detecting public IP...${NC}"
        ip=$(get_public_ip)
    fi

    local full_name="${subdomain}.${DOMAIN}"
    echo -e "${BLUE}Managing DNS record for ${full_name} -> ${ip}${NC}"

    local zone_id
    zone_id=$(get_zone_id)

    local record_id
    record_id=$(get_record_id "$zone_id" "$subdomain")

    local data
    data=$(jq -n \
        --arg type "A" \
        --arg name "$full_name" \
        --arg content "$ip" \
        --argjson proxied "$PROXIED" \
        --argjson ttl 1 \
        '{type: $type, name: $name, content: $content, proxied: $proxied, ttl: $ttl}')

    local response
    if [[ -n "$record_id" ]]; then
        echo -e "${YELLOW}Updating existing record...${NC}"
        response=$(cf_api PUT "/zones/$zone_id/dns_records/$record_id" "$data")
    else
        echo -e "${YELLOW}Creating new record...${NC}"
        response=$(cf_api POST "/zones/$zone_id/dns_records" "$data")
    fi

    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        echo -e "${GREEN}DNS record configured:${NC}"
        echo "$response" | jq '{name: .result.name, type: .result.type, content: .result.content, proxied: .result.proxied}'
    else
        echo -e "${RED}Error: Failed to configure DNS record${NC}"
        echo "$response" | jq '.errors' 2>/dev/null || echo "$response"
        exit 1
    fi
}

# Delete DNS record
cmd_delete() {
    local subdomain="$1"

    if [[ -z "$subdomain" ]]; then
        echo -e "${RED}Error: Subdomain required${NC}"
        echo "Usage: $0 delete <subdomain>"
        exit 1
    fi

    local full_name="${subdomain}.${DOMAIN}"
    echo -e "${BLUE}Deleting DNS record for ${full_name}${NC}"

    local zone_id
    zone_id=$(get_zone_id)

    local record_id
    record_id=$(get_record_id "$zone_id" "$subdomain")

    if [[ -z "$record_id" ]]; then
        echo -e "${YELLOW}Record not found: ${full_name}${NC}"
        exit 0
    fi

    local response
    response=$(cf_api DELETE "/zones/$zone_id/dns_records/$record_id")

    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        echo -e "${GREEN}DNS record deleted: ${full_name}${NC}"
    else
        echo -e "${RED}Error: Failed to delete DNS record${NC}"
        echo "$response" | jq '.errors' 2>/dev/null || echo "$response"
        exit 1
    fi
}

# List all DNS records
cmd_list() {
    echo -e "${BLUE}Listing DNS records for ${DOMAIN}${NC}"

    local zone_id
    zone_id=$(get_zone_id)

    local response
    response=$(cf_api GET "/zones/$zone_id/dns_records?per_page=100")

    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        echo "$response" | jq -r '.result[] | "\(.type)\t\(.name)\t\(.content)\t\(if .proxied then "proxied" else "dns-only" end)"' | column -t
    else
        echo -e "${RED}Error: Failed to list DNS records${NC}"
        echo "$response" | jq '.errors' 2>/dev/null || echo "$response"
        exit 1
    fi
}

# Get specific record
cmd_get() {
    local subdomain="$1"

    if [[ -z "$subdomain" ]]; then
        echo -e "${RED}Error: Subdomain required${NC}"
        echo "Usage: $0 get <subdomain>"
        exit 1
    fi

    local full_name="${subdomain}.${DOMAIN}"
    echo -e "${BLUE}Getting DNS record for ${full_name}${NC}"

    local zone_id
    zone_id=$(get_zone_id)

    local response
    response=$(cf_api GET "/zones/$zone_id/dns_records?name=$full_name")

    if echo "$response" | jq -e '.success' > /dev/null 2>&1; then
        local count
        count=$(echo "$response" | jq '.result | length')
        if [[ "$count" -eq 0 ]]; then
            echo -e "${YELLOW}No records found for ${full_name}${NC}"
        else
            echo "$response" | jq '.result[] | {name, type, content, proxied, ttl, created_on, modified_on}'
        fi
    else
        echo -e "${RED}Error: Failed to get DNS record${NC}"
        echo "$response" | jq '.errors' 2>/dev/null || echo "$response"
        exit 1
    fi
}

# Show usage
show_usage() {
    echo "Cloudflare DNS Record Manager"
    echo ""
    echo "Usage:"
    echo "  $0 add <subdomain> [ip] [--domain <domain>]    Add/update A record"
    echo "  $0 delete <subdomain> [--domain <domain>]      Delete A record"
    echo "  $0 list [--domain <domain>]                    List all DNS records"
    echo "  $0 get <subdomain> [--domain <domain>]         Get specific record details"
    echo ""
    echo "Options:"
    echo "  --domain <domain>    Use specified domain (default: pseekoo.io)"
    echo "  --no-proxy           DNS-only mode (no Cloudflare proxy)"
    echo ""
    echo "Examples:"
    echo "  $0 add qdrant                          Creates qdrant.pseekoo.io"
    echo "  $0 add api 1.2.3.4                     Creates api.pseekoo.io with specific IP"
    echo "  $0 add holylist --domain pseekoo.com   Creates holylist.pseekoo.com"
    echo "  $0 list --domain pseekoo.com           Lists pseekoo.com records"
    echo "  $0 delete qdrant                       Deletes qdrant.pseekoo.io"
}

# Parse flags from arguments
FILTERED_ARGS=()
PROXIED=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)
            DOMAIN="$2"
            shift 2
            ;;
        --no-proxy)
            PROXIED=false
            shift
            ;;
        *)
            FILTERED_ARGS+=("$1")
            shift
            ;;
    esac
done

# Main
case "${FILTERED_ARGS[0]:-}" in
    add)
        cmd_add "${FILTERED_ARGS[1]:-}" "${FILTERED_ARGS[2]:-}"
        ;;
    delete)
        cmd_delete "${FILTERED_ARGS[1]:-}"
        ;;
    list)
        cmd_list
        ;;
    get)
        cmd_get "${FILTERED_ARGS[1]:-}"
        ;;
    *)
        show_usage
        exit 1
        ;;
esac
