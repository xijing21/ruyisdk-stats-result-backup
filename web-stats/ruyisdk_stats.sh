#!/bin/bash
#
# RuyiSDK 下载量统计脚本（v3 - 增加第三方市场数据）
#
# 前置条件:
#   1. 已安装 curl, jq, date
#   2. 已配置 ~/.netrc (ES 认证)
#   3. (可选) 设置 GITHUB_TOKEN  环境变量以提高 GitHub API 速率限制
#   4. (可选) 设置 PEPY_API_KEY   环境变量以访问 PePy 累计下载量 API
#
# 用法: ./ruyisdk_stats.sh
#

# 不使用 set -e：单个数据源故障不应终止整个脚本
set -o pipefail

# ===================== 配置区 =====================

ES_BASE_URL="https://log.ams.isrc.ac.cn/isrc-public"

# 统计时间范围
START_TIME="2024-12-30T00:00:00"
END_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S")

# 输出文件
if [[ -n "${RESULT_DIR:-}" ]]; then
  mkdir -p "$RESULT_DIR"
  OUTPUT_FILE="${RESULT_DIR}/ruyisdk_stats_$(date +%Y%m%d).txt"
else
  OUTPUT_FILE="ruyisdk_stats_$(date +%Y%m%d).txt"
fi

# GitHub Token（可选）
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
PEPY_API_KEY="${PEPY_API_KEY:-}"

# PePy API Key（可选，用于 PyPI 累计下载量；无 Key 时将降级到 pypistats 近期数据）
PEPY_API_KEY="${PEPY_API_KEY:-}"

# RuyiSDK IDE 版本列表（有新版本发布时在此追加）
IDE_VERSIONS=("0.0.1" "0.0.2" "0.0.3")

# ---- 第三方市场配置 ----

# PyPI 包名
PYPI_PACKAGE="ruyi"

# Visual Studio Marketplace: 发布者ID / 扩展ID
VSCODE_MKT_PUBLISHER="RuyiSDK"
VSCODE_MKT_EXTENSION="ruyisdk-vscode-extension"

# Open VSX Registry: 命名空间 / 扩展名
OPENVSX_NAMESPACE="ruyisdk"
OPENVSX_EXTENSION="ruyisdk-vscode-extension"

# Eclipse Marketplace: 解决方案节点 ID（暂未确定，留空表示不可用 → 记为 N/A / 0）
ECLIPSE_MKT_ID=""

# 网络请求参数
CURL_TIMEOUT=30
CURL_RETRY=2

# ===================== 前置检查 =====================

if [[ ! -f ~/.netrc ]]; then
  echo "警告: 未找到 ~/.netrc 文件，ES 查询将失败。" >&2
  echo "警告: 在 GitHub Actions 中请确保已配置 ES_NETRC Secret。" >&2
fi
if [[ -f ~/.netrc ]] && [[ $(stat -c %a ~/.netrc 2>/dev/null || stat -f %Lp ~/.netrc 2>/dev/null) != "600" ]]; then
  echo "警告: ~/.netrc 权限非 600，curl 可能拒绝使用。" >&2
fi

# ===================== 函数定义 =====================

# ---------------------------------------------------------------
# sanitize_num: 将 QUERY_FAILED / 空 / 非数字 安全转换为整数 0
# 用途：算术运算前对所有来源值做防护
# ---------------------------------------------------------------
sanitize_num() {
    local val="${1:-}"
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
    elif [[ "$val" =~ ^[0-9]+\.[0-9]+$ ]]; then
        # 浮点数：截断小数部分（87.0 → 87）
        echo "${val%.*}"
    else
        echo "0"
    fi
}



# ---------------------------------------------------------------
# es_count: 查询 ES，按 wildcard 模式统计文档数
# 返回：非负整数 或 "QUERY_FAILED"
# ---------------------------------------------------------------
es_count() {
    local pattern="$1"
    local response

    response=$(curl -n -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
        -X GET "${ES_BASE_URL}/_count" \
        -H 'Content-Type: application/json' \
        -d "{
            \"query\": {
                \"bool\": {
                    \"must\": [
                        {
                            \"wildcard\": {
                                \"url.path\": {
                                    \"value\": \"${pattern}\"
                                }
                            }
                        },
                        {
                            \"range\": {
                                \"@timestamp\": {
                                    \"gte\": \"${START_TIME}\",
                                    \"lt\": \"${END_TIME}\"
                                }
                            }
                        }
                    ]
                }
            }
        }" 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${response:-}" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    local count
    count=$(echo "$response" | jq -r '.count // "QUERY_FAILED"' 2>/dev/null) \
        || { echo "QUERY_FAILED"; return; }

    if [[ -z "${count:-}" || "$count" == "null" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    echo "${count}"
}

# ---------------------------------------------------------------
# github_release_downloads: 某仓库所有 Release Assets 累计下载量
# 返回：非负整数 或 "QUERY_FAILED"（仅首页失败时）
# ---------------------------------------------------------------
github_release_downloads() {
    local repo="$1"
    local total=0
    local page=1

    while true; do
        local url="https://api.github.com/repos/${repo}/releases?page=${page}&per_page=100"
        local response

        if [[ -n "${GITHUB_TOKEN}" ]]; then
            response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
                -H "Authorization: token ${GITHUB_TOKEN}" \
                -H "Accept: application/vnd.github+json" \
                "$url" 2>/dev/null) || { echo "QUERY_FAILED"; return; }
        else
            response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
                -H "Accept: application/vnd.github+json" \
                "$url" 2>/dev/null) || { echo "QUERY_FAILED"; return; }
        fi

        if [[ -z "${response:-}" ]]; then
            echo "QUERY_FAILED"
            return
        fi

        # 检测 API 错误（如速率限制）
        if echo "$response" | jq -e '.message' &>/dev/null; then
            local msg
            msg=$(echo "$response" | jq -r '.message // "unknown error"' 2>/dev/null)
            echo "警告: GitHub API 查询 ${repo} 出错 (page=${page}): ${msg}" >&2
            # 首页即失败 → 整体标记失败；后续页失败则返回已累加值
            if [[ $page -eq 1 ]]; then
                echo "QUERY_FAILED"
                return
            else
                break
            fi
        fi

        local page_len
        page_len=$(echo "$response" | jq '. | length' 2>/dev/null) || page_len=0

        # 空数组则结束
        if [[ "$page_len" -eq 0 || "$page_len" == "null" ]]; then
            break
        fi

        # 累加本页所有 asset 的 download_count
        local page_downloads
        page_downloads=$(echo "$response" | jq '[.[].assets[].download_count // 0] | add // 0' 2>/dev/null) || page_downloads=0
        total=$((total + page_downloads))

        # 不足 100 条说明是最后一页
        if [[ "$page_len" -lt 100 ]]; then
            break
        fi

        page=$((page + 1))

        # 安全阀
        if [[ $page -gt 20 ]]; then
            echo "警告: ${repo} Release 页数超过 20，可能有数据未统计。" >&2
            break
        fi
    done

    echo "${total}"
}

# ---------------------------------------------------------------
# pypi_downloads: 通过 PePy API 获取 PyPI 包累计下载量
#   - 有 PEPY_API_KEY 时带 Key 请求
#   - 失败时降级到 pypistats.org（仅含近期数据，非完整累计）
# 返回：非负整数 或 "QUERY_FAILED"
# ---------------------------------------------------------------
pypi_downloads() {
  local package="$1"
  local url="https://api.pepy.tech/api/v1/projects/${package}"
  local max_retries=3
  local retry_delay=5
  
  for ((i=1; i<=max_retries; i++)); do
    local response=$(curl -s -w "\n%{http_code}" --max-time 30 \
      -H "X-API-Key: ${PEPY_API_KEY}" \
      "$url" 2>/dev/null)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    if [[ "${http_code}" == "200" ]]; then
      local total=$(echo "$body" | jq -r '
        if .total_downloads != null then .total_downloads
        elif .downloads != null then .downloads
        else "QUERY_FAILED" end
      ' 2>/dev/null)
      
      if [[ -n "$total" && "$total" != "null" && "$total" != "QUERY_FAILED" ]]; then
        echo "${total}"
        return 0
      fi
    fi
    
    echo "⚠️ PePy API 尝试 ${i}/${max_retries} 失败 (HTTP ${http_code})，${retry_delay}秒后重试..." >&2
    sleep ${retry_delay}
  done
  
  # 所有重试失败，降级到 pypistats
  echo "❌ PePy API 全部重试失败，降级到 pypistats.org" >&2
  _pypi_downloads_pypistats "$package"
}


# 降级方案：pypistats.org（仅近期数据，非完整累计）
_pypi_downloads_pypistats() {
    local package="$1"
    local url="https://pypistats.org/api/packages/${package}/overall"
    local response

    response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
        -H "Accept: application/json" \
        "$url" 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${response:-}" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    # 使用 with_mirrors 类别（包含镜像下载量，数值更接近真实总量）
    local total
    total=$(echo "$response" | jq '
        [.data[] | select(.category == "with_mirrors") | .downloads // 0]
        | add // 0
    ' 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${total:-}" || "$total" == "null" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    echo "  注意: 以下 PyPI 数据来自 pypistats.org 降级方案，仅包含近期数据，非完整累计" >&2
    echo "${total}"
}


# ---------------------------------------------------------------
# openvsx_downloads: 查询 Open VSX Registry 扩展累计下载量
# 返回：非负整数 或 "QUERY_FAILED"
# 注：字段名 downloadCount 需按实际 API 返回调整
# ---------------------------------------------------------------
openvsx_downloads() {
    local namespace="$1"
    local extension="$2"
    local url="https://open-vsx.org/api/${namespace}/${extension}"
    local response

    response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
        "$url" 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${response:-}" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    # 检测错误
    if echo "$response" | jq -e '.error' &>/dev/null 2>&1; then
        local msg
        msg=$(echo "$response" | jq -r '.error.message // .error // "unknown error"' 2>/dev/null)
        echo "警告: Open VSX 查询 ${namespace}.${extension} 出错: ${msg}" >&2
        echo "QUERY_FAILED"
        return
    fi

    local count
    count=$(echo "$response" | jq -r '.downloadCount // "QUERY_FAILED"' 2>/dev/null) \
        || { echo "QUERY_FAILED"; return; }

    if [[ -z "${count:-}" || "$count" == "null" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    echo "${count}"
}

# ---------------------------------------------------------------
# vscode_marketplace_installs: 查询 Visual Studio Marketplace 安装量
# 返回：非负整数 或 "QUERY_FAILED"
# 注：使用非官方 API，可能随微软调整而失效
# ---------------------------------------------------------------
vscode_marketplace_installs() {
    local publisher="$1"
    local extension="$2"
    local url="https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"

    # 用 jq 构建请求体，避免转义问题
    local request_body
    request_body=$(jq -n \
        --arg pe "${publisher}.${extension}" \
        '{
            filters: [{
                criteria: [
                    {filterType: 7, value: $pe}
                ]
            }],
            assetTypes: [],
            flags: 402
        }' 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    local response
    response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
        -X POST "$url" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json;api-version=6.1-preview.1' \
        -d "$request_body" 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${response:-}" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    local count
    count=$(echo "$response" | jq -r '
        if .results[0].extensions[0].statistics then
            [.results[0].extensions[0].statistics[]
             | select(.statisticName == "install")
             | .value][0] // "QUERY_FAILED"
        else
            "QUERY_FAILED"
        end
    ' 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${count:-}" || "$count" == "null" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    echo "${count}"
}

# ---------------------------------------------------------------
# eclipse_marketplace_installs: 查询 Eclipse Marketplace 安装量
# 返回：非负整数 或 "QUERY_FAILED"
# 注：ECLIPSE_MKT_ID 为空时直接返回 QUERY_FAILED（对应网页记为 N/A → 0）
# ---------------------------------------------------------------
eclipse_marketplace_installs() {
    local solution_id="$1"

    if [[ -z "${solution_id}" ]]; then
        echo "警告: Eclipse Marketplace 解决方案 ID 未配置，跳过查询。" >&2
        echo "QUERY_FAILED"
        return
    fi

    local url="https://marketplace.eclipse.org/node/${solution_id}/stats.json"
    local response

    response=$(curl -s --max-time "${CURL_TIMEOUT}" --retry "${CURL_RETRY}" \
        "$url" 2>/dev/null) || { echo "QUERY_FAILED"; return; }

    if [[ -z "${response:-}" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    local count
    count=$(echo "$response" | jq -r '.installstotal // "QUERY_FAILED"' 2>/dev/null) \
        || { echo "QUERY_FAILED"; return; }

    if [[ -z "${count:-}" || "$count" == "null" ]]; then
        echo "QUERY_FAILED"
        return
    fi

    echo "${count}"
}

# ===================== 开始统计 =====================
# 输出同时到终端和文件
exec > >(tee -a "$OUTPUT_FILE") 2>&1

echo "=========================================="
echo " RuyiSDK 下载量统计（v3 - 增加第三方市场数据）"
echo "=========================================="
echo "统计时间范围: ${START_TIME} ~ ${END_TIME}"
echo "输出文件: ${OUTPUT_FILE}"
echo ""

# ---------- [1/4] ES 统计 ----------

echo "[1/4] 正在查询 Elasticsearch ..."

s_ruyisdk=$(es_count "/ruyisdk/*")
s_ruyisdk_dist=$(es_count "/ruyisdk/dist/*")
s_ruyisdk_humans=$(es_count "/ruyisdk/humans/*")
s_ruyisdk_ruyi=$(es_count "/ruyisdk/ruyi/*")

# RuyiSDK IDE：各版本分别查询后求和
s_ruyisdk_ide=0
declare -A ide_version_counts
for ver in "${IDE_VERSIONS[@]}"; do
    cnt=$(es_count "/ruyisdk/ide/${ver}/*")
    ide_version_counts["${ver}"]="${cnt}"
    if [[ "${cnt}" != "QUERY_FAILED" ]]; then
        s_ruyisdk_ide=$((s_ruyisdk_ide + cnt))
    else
        echo "  警告: ES 查询 /ruyisdk/ide/${ver}/* 失败，跳过该版本。" >&2
    fi
done

s_ruyisdk_eclipse=$(es_count "/ruyisdk/ide/plugins/eclipse/*")
s_ruyisdk_vscode=$(es_count "/ruyisdk/ide/plugins/vscode/*")
s_ruyisdk_3rdparty=$(es_count "/ruyisdk/3rdparty/*")

# ---------- [2/4] GitHub 统计 ----------

echo ""
echo "[2/4] 正在查询 GitHub Releases ..."

g_ruyi=$(github_release_downloads "ruyisdk/ruyi")
g_ide=$(github_release_downloads "ruyisdk/ruyisdk-eclipse-packages")
g_eclipse=$(github_release_downloads "ruyisdk/ruyisdk-eclipse-plugins")
g_vscode=$(github_release_downloads "ruyisdk/ruyisdk-vscode-extension")

# ---------- [3/4] 第三方市场统计 ----------

echo ""
echo "[3/4] 正在查询第三方应用市场 ..."

pypi_ruyi=$(pypi_downloads "${PYPI_PACKAGE}")
vsx_vscode=$(openvsx_downloads "${OPENVSX_NAMESPACE}" "${OPENVSX_EXTENSION}")
m_vscode=$(vscode_marketplace_installs "${VSCODE_MKT_PUBLISHER}" "${VSCODE_MKT_EXTENSION}")
m_eclipse=$(eclipse_marketplace_installs "${ECLIPSE_MKT_ID}")

# ---------- [4/4] 计算网页汇总数据 ----------

echo ""
echo "[4/4] 正在汇总数据 ..."

# 所有参与算术的值先经 sanitize_num 防护

# 1. 网页：RuyiSDK 组件包下载量 = ES: ruyisdk/dist/
web_components=$(sanitize_num "$s_ruyisdk_dist")

# 2. 网页：RuyiSDK 文档下载量 = ES: ruyisdk/humans/
web_docs=$(sanitize_num "$s_ruyisdk_humans")

# 3. 网页：RuyiSDK 包管理器下载量 = ES: ruyisdk/ruyi/ + GitHub: ruyi + PyPI: ruyi
web_ruyi_manager=$(( $(sanitize_num "$s_ruyisdk_ruyi") + $(sanitize_num "$g_ruyi") + $(sanitize_num "$pypi_ruyi") ))

# 4. 网页：RuyiSDK VS Code 插件下载量 = ES: vscode/ + GitHub: VSCode + Open VSX + VS Code Marketplace
web_vscode=$(( $(sanitize_num "$s_ruyisdk_vscode") + $(sanitize_num "$g_vscode") + $(sanitize_num "$vsx_vscode") + $(sanitize_num "$m_vscode") ))

# 5. 网页：RuyiSDK Eclipse 组件下载量 = ES: ide/ + GitHub: IDE(packages) + ES: eclipse/ + GitHub: Eclipse + Eclipse Marketplace
web_eclipse=$(( $(sanitize_num "$s_ruyisdk_ide") + $(sanitize_num "$g_ide") + $(sanitize_num "$s_ruyisdk_eclipse") + $(sanitize_num "$g_eclipse") + $(sanitize_num "$m_eclipse") ))

# ===================== 生成报告 =====================

echo ""
echo "正在生成报告 ..."

{
    echo "============================================================"
    echo "  RuyiSDK 下载量统计报告"
    echo "============================================================"
    echo ""
    echo "【1. 统计起止时间】"
    echo "  起始时间: ${START_TIME} (ES 中最早数据时间: 2024-12-30)"
    echo "  截止时间: ${END_TIME}"
    echo ""
    echo "【2. 统计执行命令】"
    echo ""
    echo "  --- Elasticsearch 查询 ---"
    echo "  命令格式:"
    echo "  curl -n -s -X GET \"${ES_BASE_URL}/_count\" \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '<JSON_BODY>'"
    echo ""
    echo "  JSON_BODY 示例 (pattern 替换为实际通配符):"
    cat <<'ESJSON'
  {
    "query": {
      "bool": {
        "must": [
          { "wildcard": { "url.path": { "value": "<pattern>" } } },
          { "range":   { "@timestamp": { "gte": "<START>", "lt": "<END>" } } }
        ]
      }
    }
  }
ESJSON
    echo ""
    echo "  本次实际参数:"
    echo "    START = ${START_TIME}"
    echo "    END   = ${END_TIME}"
    echo ""
    echo "  查询的 pattern 列表:"
    echo "    /ruyisdk/*"
    echo "    /ruyisdk/dist/*"
    echo "    /ruyisdk/humans/*"
    echo "    /ruyisdk/ruyi/*"
    for ver in "${IDE_VERSIONS[@]}"; do
        echo "    /ruyisdk/ide/${ver}/*"
    done
    echo "    /ruyisdk/ide/plugins/eclipse/*"
    echo "    /ruyisdk/ide/plugins/vscode/*"
    echo "    /ruyisdk/3rdparty/*"
    echo ""
    echo "  说明: wildcard 的 * 会递归匹配所有层级子路径（含 / 分隔符）"
    echo ""
    echo "  --- GitHub Release 查询 ---"
    echo "  命令格式:"
    if [[ -n "${GITHUB_TOKEN}" ]]; then
        echo "  curl -s -H 'Authorization: token <TOKEN>' \\"
    else
        echo "  curl -s \\"
    fi
    echo "    -H 'Accept: application/vnd.github+json' \\"
    echo "    'https://api.github.com/repos/<REPO>/releases?page=<N>&per_page=100'"
    echo ""
    echo "  查询的仓库:"
    echo "    ruyisdk/ruyi"
    echo "    ruyisdk/ruyisdk-eclipse-packages"
    echo "    ruyisdk/ruyisdk-eclipse-plugins"
    echo "    ruyisdk/ruyisdk-vscode-extension"
    echo ""
    echo "  --- 第三方应用市场查询 ---"
    echo ""
    echo "  PyPI (PePy API):"
    echo "    curl -s [-H 'X-API-Key: <KEY>'] \\"
    echo "      'https://api.pepy.tech/api/v1/projects/${PYPI_PACKAGE}'"
    echo "    降级方案 (pypistats.org，仅近期数据):"
    echo "    curl -s 'https://pypistats.org/api/packages/${PYPI_PACKAGE}/overall'"
    echo ""
    echo "  Open VSX Registry:"
    echo "    curl -s 'https://open-vsx.org/api/${OPENVSX_NAMESPACE}/${OPENVSX_EXTENSION}'"
    echo ""
    echo "  Visual Studio Marketplace:"
    echo "    POST https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
    echo "    查询: ${VSCODE_MKT_PUBLISHER}.${VSCODE_MKT_EXTENSION}"
    echo ""
    echo "  Eclipse Marketplace:"
    if [[ -n "${ECLIPSE_MKT_ID}" ]]; then
        echo "    curl -s 'https://marketplace.eclipse.org/node/${ECLIPSE_MKT_ID}/stats.json'"
    else
        echo "    (未配置解决方案 ID，跳过查询)"
    fi
    echo ""
    echo "============================================================"
    echo ""
    echo "【3. 统计结果】"
    echo ""
    echo "--- ES 日志统计（镜像站下载量，截至 ${END_TIME}）---"
    echo ""
    echo "ruyisdk/ 总下载量:                               ${s_ruyisdk}"
    echo "ruyisdk/dist/ 基础组件包下载量:                   ${s_ruyisdk_dist}"
    echo "ruyisdk/humans/ 文档/视频等资源下载量:             ${s_ruyisdk_humans}"
    echo "ruyisdk/ruyi/ ruyi包管理器工具总下载量:            ${s_ruyisdk_ruyi}"
    echo "ruyisdk/ide/ RuyiSDK IDE 总下载量:                ${s_ruyisdk_ide}"
    for ver in "${IDE_VERSIONS[@]}"; do
        printf "  - ruyisdk/ide/%s/:  %s\n" "${ver}" "${ide_version_counts[${ver}]}"
    done
    echo "ruyisdk/ide/plugins/eclipse/ Eclipse插件下载量:    ${s_ruyisdk_eclipse}"
    echo "ruyisdk/ide/plugins/vscode/ VSCode插件下载量:      ${s_ruyisdk_vscode}"
    echo "ruyisdk/3rdparty/ 第三方资源下载量:                 ${s_ruyisdk_3rdparty}"
    echo ""
    echo "  [数据一致性验证]"
    echo "  已统计子项之和: $(( $(sanitize_num "$s_ruyisdk_dist") + $(sanitize_num "$s_ruyisdk_humans") + $(sanitize_num "$s_ruyisdk_ruyi") + $(sanitize_num "$s_ruyisdk_ide") + $(sanitize_num "$s_ruyisdk_eclipse") + $(sanitize_num "$s_ruyisdk_vscode") + $(sanitize_num "$s_ruyisdk_3rdparty") ))"
    echo "  ruyisdk/ 总量:  ${s_ruyisdk}"
    echo "  未归类差值:     $(( $(sanitize_num "$s_ruyisdk") - ($(sanitize_num "$s_ruyisdk_dist") + $(sanitize_num "$s_ruyisdk_humans") + $(sanitize_num "$s_ruyisdk_ruyi") + $(sanitize_num "$s_ruyisdk_ide") + $(sanitize_num "$s_ruyisdk_eclipse") + $(sanitize_num "$s_ruyisdk_vscode") + $(sanitize_num "$s_ruyisdk_3rdparty")) ))"
    echo ""
    echo "--- GitHub Release 统计（Assets 累计下载量）---"
    echo ""
    echo "ruyi 工具累计下载量 (ruyisdk/ruyi):                       ${g_ruyi}"
    echo "IDE 包累计下载量 (ruyisdk/ruyisdk-eclipse-packages):      ${g_ide}"
    echo "Eclipse 插件累计下载量 (ruyisdk/ruyisdk-eclipse-plugins): ${g_eclipse}"
    echo "VSCode 插件累计下载量 (ruyisdk/ruyisdk-vscode-extension): ${g_vscode}"
    echo ""
    echo "--- 第三方应用市场统计（累计下载/安装量）---"
    echo ""
    echo "PyPI ${PYPI_PACKAGE} 累计下载量:                                ${pypi_ruyi}"
    echo "Open VSX ${OPENVSX_NAMESPACE}.${OPENVSX_EXTENSION} 累计下载量:   ${vsx_vscode}"
    echo "Visual Studio Marketplace ${VSCODE_MKT_PUBLISHER}.${VSCODE_MKT_EXTENSION} 安装量: ${m_vscode}"
    if [[ -n "${ECLIPSE_MKT_ID}" ]]; then
        echo "Eclipse Marketplace (ID:${ECLIPSE_MKT_ID}) 安装量:           ${m_eclipse}"
    else
        echo "Eclipse Marketplace 安装量:                                   N/A（未配置 ID，按 0 计入）"
    fi
    echo ""
    echo "--- 网页统计数据（对比参考）---"
    echo ""
    echo "网页：RuyiSDK 组件包下载量:                       ${web_components}"
    echo "  = ES镜像(ruyisdk/dist/) ${s_ruyisdk_dist}"
    echo ""
    echo "网页：RuyiSDK 文档下载量:                         ${web_docs}"
    echo "  = ES镜像(ruyisdk/humans/) ${s_ruyisdk_humans}"
    echo ""
    echo "网页：RuyiSDK 包管理器下载量:                     ${web_ruyi_manager}"
    echo "  = ES镜像(ruyisdk/ruyi/) ${s_ruyisdk_ruyi} + GitHub(ruyi) ${g_ruyi} + PyPI ${pypi_ruyi}"
    echo ""
    echo "网页：RuyiSDK VS Code 插件下载量:                 ${web_vscode}"
    echo "  = ES镜像(vscode/) ${s_ruyisdk_vscode} + GitHub(vscode) ${g_vscode} + OpenVSX ${vsx_vscode} + VSCode Marketplace ${m_vscode}"
    echo ""
    echo "网页：RuyiSDK Eclipse 组件下载量:                 ${web_eclipse}"
    echo "  = ES镜像(ide/) ${s_ruyisdk_ide} + GitHub(IDE packages) ${g_ide} + ES镜像(eclipse/) ${s_ruyisdk_eclipse} + GitHub(eclipse) ${g_eclipse} + Eclipse Marketplace ${m_eclipse}"
    echo ""
    echo "============================================================"
    echo "  报告生成时间: $(date '+%Y/%m/%d %H:%M:%S')"
    echo "============================================================"
}

echo ""
echo "✅ 统计报告已保存至: ${OUTPUT_FILE}"
