<?php
// get-paper.php
$password = 'changeme'; // set your password here
$email = 'some@email.com'; // change if desired

$scihub_domains = [
    'https://sci-hub.ru',
    'https://sci-hub.st',
    'https://sci-hub.se',
    'https://sci-hub.su',
    'https://sci-hub.al',
    'https://sci-hub.mk',
    'https://sci-hub.ee',
    
];

ini_set('memory_limit', '256M');
ini_set('max_execution_time', 120);

header('X-Content-Type-Options: nosniff');

function no_cache() {
    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    header('Pragma: no-cache');
    header('Expires: 0');
}

if (!isset($_GET['password']) || $_GET['password'] !== $password) {
    http_response_code(403);
    no_cache();
    header('Content-Type: text/plain; charset=utf-8');
    echo 'Forbidden';
    exit;
}

$id = isset($_GET['id']) ? trim($_GET['id']) : (isset($_GET['doi']) ? trim($_GET['doi']) : '');

if ($id === '') {
    no_cache();
    header('Content-Type: text/plain; charset=utf-8');
    echo "API\n";
    echo "GET ?password=YOURPASS&id=DOI_OR_ARXIV\n";
    echo "  or ?password=YOURPASS&doi=10.xxxx/xxxx\n";
    echo "Returns: application/pdf on success, or plain error text.\n";
    echo "Sources tried (in order): arXiv, Unpaywall, other OA mirrors.\n";
    exit;
}

function http_get($url, $timeout = 45) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS => 8,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_CONNECTTIMEOUT => 15,
        CURLOPT_USERAGENT => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => false,
        CURLOPT_ENCODING => '',
        CURLOPT_HTTPHEADER => [
            'Accept: application/pdf,text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language: en-US,en;q=0.9',
        ],
    ]);
    $body = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $ctype = (string)curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    $final = (string)curl_getinfo($ch, CURLINFO_EFFECTIVE_URL);
    curl_close($ch);
    return [$body, $code, $ctype, $final];
}

function is_pdf($data, $ctype = '') {
    if ($data === false || $data === '' || strlen($data) < 5) return false;
    if (strncmp($data, '%PDF', 4) === 0) return true;
    if ($ctype && stripos($ctype, 'pdf') !== false) return true;
    return false;
}

function extract_doi($s) {
    if (preg_match('/(10\.\d{2,9}\/[^\s"<>]+)/', $s, $m)) {
        return rtrim($m[1], '.,;');
    }
    return null;
}

function extract_arxiv($s) {
    if (preg_match('/(?:arxiv[:\s]|10\.48550\/arXiv\.)(\d{4}\.\d{4,5})(v\d+)?/i', $s, $m)) {
        return $m[1] . (isset($m[2]) ? $m[2] : '');
    }
    if (preg_match('/^(\d{4}\.\d{4,5})(v\d+)?$/i', $s, $m)) {
        return $m[1] . (isset($m[2]) ? $m[2] : '');
    }
    return null;
}

$doi = extract_doi($id);
$arxiv = extract_arxiv($id);

// 1. arXiv direct
if ($arxiv) {
    $url = "https://arxiv.org/pdf/{$arxiv}.pdf";
    list($body, $code, $ctype) = http_get($url, 60);
    if ($code >= 200 && $code < 300 && is_pdf($body, $ctype)) {
        header('Content-Type: application/pdf');
        header('Content-Disposition: inline; filename="' . $arxiv . '.pdf"');
        echo $body;
        exit;
    }
}

// 2. Unpaywall
if ($doi) {
    $url = 'https://api.unpaywall.org/v2/' . rawurlencode($doi) . '?email=' . rawurlencode($email);
    list($json, $code) = http_get($url, 20);
    if ($code == 200 && $json) {
        $data = json_decode($json, true);
        $candidates = [];
        if (!empty($data['best_oa_location']['url_for_pdf'])) {
            $candidates[] = $data['best_oa_location']['url_for_pdf'];
        }
        if (!empty($data['best_oa_location']['url'])) {
            $candidates[] = $data['best_oa_location']['url'];
        }
        foreach ($candidates as $pdf_url) {
            list($body, $code, $ctype) = http_get($pdf_url, 60);
            if ($code >= 200 && $code < 300 && is_pdf($body, $ctype)) {
                header('Content-Type: application/pdf');
                header('Content-Disposition: inline; filename="' . preg_replace('/[^a-zA-Z0-9._-]/', '_', $doi) . '.pdf"');
                echo $body;
                exit;
            }
        }
    }
}

// 2b. IEEE Xplore
if ($doi && preg_match('/^10\.1109\//i', $doi)) {
    list($html, $code, $ctype, $final) = http_get('https://doi.org/' . rawurlencode($doi), 20);
    $arnumber = null;
    if (!empty($final) && preg_match('#/document/(\d+)#', $final, $m)) {
        $arnumber = $m[1];
    } elseif (preg_match('#ieeexplore\.ieee\.org/document/(\d+)#i', $html, $m)) {
        $arnumber = $m[1];
    } elseif (preg_match('/"articleNumber"\s*:\s*"?(\d+)"?/i', $html, $m)) {
        $arnumber = $m[1];
    }
    if ($arnumber) {
        $pdf_url = 'https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=' . $arnumber;
        list($body, $code, $ctype) = http_get($pdf_url, 60);
        if ($code >= 200 && $code < 300 && is_pdf($body, $ctype)) {
            header('Content-Type: application/pdf');
            header('Content-Disposition: inline; filename="' . preg_replace('/[^a-zA-Z0-9._-]/', '_', $doi) . '.pdf"');
            echo $body;
            exit;
        }
    }
}

// 3. other mirrors
if ($doi) {
    foreach ($scihub_domains as $base) {
        $base = rtrim($base, '/');
        $candidates = [
            $base . '/' . $doi,
            $base . '/' . rawurlencode($doi),
        ];
        foreach ($candidates as $url) {
            list($html, $code) = http_get($url, 20);
            if ($code < 200 || $code >= 400 || !$html) continue;

            if (is_pdf($html)) {
                header('Content-Type: application/pdf');
                header('Content-Disposition: inline; filename="' . preg_replace('/[^a-zA-Z0-9._-]/', '_', $doi) . '.pdf"');
                echo $html;
                exit;
            }

            $pdf_url = null;
            if (preg_match('/<embed[^>]+src=["\']([^"\']+)["\']/i', $html, $m)) {
                $pdf_url = $m[1];
            } elseif (preg_match('/<iframe[^>]+src=["\']([^"\']+)["\']/i', $html, $m)) {
                $pdf_url = $m[1];
            } elseif (preg_match('/location\.href\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']/i', $html, $m)) {
                $pdf_url = $m[1];
            } elseif (preg_match('/<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:save|download|pdf)/i', $html, $m)) {
                $pdf_url = $m[1];
            }

            if ($pdf_url) {
                if (strpos($pdf_url, '//') === 0) $pdf_url = 'https:' . $pdf_url;
                elseif (strpos($pdf_url, '/') === 0) $pdf_url = $base . $pdf_url;
                elseif (!preg_match('#^https?://#i', $pdf_url)) $pdf_url = $base . '/' . ltrim($pdf_url, '/');

                list($body, $code, $ctype) = http_get($pdf_url, 60);
                if ($code >= 200 && $code < 300 && is_pdf($body, $ctype)) {
                    header('Content-Type: application/pdf');
                    header('Content-Disposition: inline; filename="' . preg_replace('/[^a-zA-Z0-9._-]/', '_', $doi) . '.pdf"');
                    echo $body;
                    exit;
                }
            }
        }
    }
}

http_response_code(404);
no_cache();
header('Content-Type: text/plain; charset=utf-8');
echo 'Not found';
