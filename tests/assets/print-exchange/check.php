<?php
declare(strict_types=1);

use Phpdftk\Pdf\Conformance\ConformanceChecker;
use Phpdftk\Pdf\Conformance\Profile\PdfVtProfile;
use Phpdftk\Pdf\Conformance\Profile\PdfXProfile;

require $argv[1];
$profile = $argv[3] === 'x4' ? PdfXProfile::X4 : PdfVtProfile::from('VT-1');
$result = ConformanceChecker::open($argv[2])->checkProfile($profile);
echo json_encode([
    'compliant' => $result->isCompliant,
    'rules' => array_column($result->violations, 'clause'),
], JSON_THROW_ON_ERROR), "\n";
