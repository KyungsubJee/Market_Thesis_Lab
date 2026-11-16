# 실행 옵션

[프로젝트 README](../README.md)의 설치 안내를 먼저 따르세요. 아래 명령은 저장소 루트에서 실행합니다. 출력은 새 디렉터리를 사용합니다.

| 실행 대상 | 명령 |
| --- | --- |
| 보유 분석과 신규 후보 | `python -B -m research_prototype.integrated --holdings research_prototype/examples/usa.csv --output-dir runs/demo` |
| 고정 자료 보유 분석 | `python -B -m research_prototype --holdings research_prototype/examples/samsung.csv --output-dir runs/fixed` |
| 미국 실수집 보유 분석 | `python -B -m research_prototype --source us-live --holdings research_prototype/examples/usa.csv --output-dir runs/held` |
| SOXL 단일 종목 점검 | `python -B -m research_prototype --week3 --source us-live --holdings research_prototype/examples/soxl.csv --audit-inputs --output-dir runs/soxl` |
| 미국 신규 후보 | `python -B -m research_prototype.discovery --holdings research_prototype/examples/discovery-demo.csv --auto-qualitative --output-dir runs/discovery` |

통합 실행은 `--collect-only`, 신규 후보 실행은 `--screen-only`로 모델 없이 데이터 수집과 선별을 확인할 수 있습니다. `--model`과 `--timeout`으로 모델과 호출 제한 시간을 설정합니다.

## 저장된 실행 다시 분석하기

- 통합 실행에서 `--replay <evidence.json>`을 사용하면 기준 시각·정량 데이터·후보 순위를 재사용합니다. `--retry-discovery <evidence.json>`은 실패한 신규 후보 모델 호출만 다시 실행합니다.
- 통합 실행의 `--event-sources <file.json>`은 기준 시각까지 공개된 기업 발표와 독립 보도를 지정합니다. `schema_version`은 `1`이며, `sources` 배열의 각 항목은 `ticker`, HTTPS `url`, `kind` (`issuer` 또는 `independent_news`), `title_pattern`, 시간대가 있는 `published_at`을 담습니다. 날짜만 확인됐으면 공개 시각의 보수적 상한과 `availability_basis: date_upper_bound`를 사용합니다. 정확한 시각을 확인했으면 `verified_exact_timestamp`를 지정합니다.
- 신규 후보 실행의 `--qualitative-registry`는 [기본 출처 설정](discovery_sources.json)을 교체합니다. `--replay-screen <evidence.json>`과 `--auto-qualitative`를 함께 쓰면 선별을 재사용하고 정성 출처를 다시 조회합니다.

시장 가격은 배당이 포함되지 않은 수익률을 사용합니다. 독립 기사와 기업 발표는 공개 시점과 출처를 점검하고, 해당 종목에 허용된 근거를 모델에 전달합니다. 기사 본문에 접근하지 못하면 이 상태를 실행 기록에 남깁니다.
