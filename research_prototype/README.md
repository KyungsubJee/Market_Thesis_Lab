# 실행 옵션

[프로젝트 README](../README.md)의 설치 안내를 먼저 따르세요. 아래 명령은 저장소 루트에서 실행합니다. 출력은 새 디렉터리를 사용합니다.

| 실행 대상 | 명령 |
| --- | --- |
| 보유 분석과 신규 후보 | `python -B -m research_prototype.integrated --holdings research_prototype/examples/usa.csv --output-dir runs/demo` |
| 고정 자료 보유 분석 | `python -B -m research_prototype --holdings research_prototype/examples/samsung.csv --output-dir runs/fixed` |
| 미국 실수집 보유 분석 | `python -B -m research_prototype --source us-live --holdings research_prototype/examples/usa.csv --output-dir runs/held` |
| SOXL 단일 종목 점검 | `python -B -m research_prototype --week3 --source us-live --holdings research_prototype/examples/soxl.csv --audit-inputs --output-dir runs/soxl` |
| 미국 신규 후보 | `python -B -m research_prototype.discovery --holdings research_prototype/examples/discovery-demo.csv --auto-qualitative --output-dir runs/discovery` |
| 저장된 분석에 공개 정성자료 추가 | `python -B -m research_prototype.integrated --qualitative-replay runs/demo/evidence.json --output-dir runs/demo-public` |

통합 실행은 `--collect-only`, 신규 후보 실행은 `--screen-only`로 모델 없이 데이터 수집과 선별을 확인할 수 있습니다. `--model`과 `--timeout`으로 모델과 호출 제한 시간을 설정합니다.

## 저장된 실행 다시 분석하기

- 통합 실행에서 `--replay <evidence.json>`을 사용하면 기준 시각·정량 데이터·후보 순위를 재사용합니다. `--retry-discovery <evidence.json>`은 실패한 신규 후보 모델 호출만 다시 실행합니다.
- 통합 실행의 공개 정성자료 수집은 미국·한국 구글 뉴스 RSS와 Bluesky 공개 검색을 사용합니다. 출처 계정이나 API 키가 필요 없으며, 기존 환경 변수에 설정된 인증 정보도 사용하지 않습니다. 조회 실패와 검색 범위 내 적격 자료 0건을 구분합니다. 뉴스 검색은 기사 제목과 출처를 가져오며, 공개 커뮤니티 글은 전체 투자자의 관점을 대표하지 않습니다.
- `--qualitative-replay <evidence.json>`으로 기존 판단과 순위를 유지한 채 공개 자료를 다시 수집합니다. 필요할 때만 `--qualitative-sources <file.json>`에 검토한 공개 링크를 추가할 수 있습니다. `--qualitative-as-of`는 시간대를 포함한 수집 기준 시각을 지정하며, `--collect-only`는 모델 없이 공개 수집 결과만 확인합니다.
- 통합 실행의 `--event-sources <file.json>`은 기준 시각까지 공개된 기업 발표와 독립 보도를 지정합니다. `schema_version`은 `1`이며, `sources` 배열의 각 항목은 `ticker`, HTTPS `url`, `kind` (`issuer` 또는 `independent_news`), `title_pattern`, 시간대가 있는 `published_at`을 담습니다. 날짜만 확인됐으면 공개 시각의 보수적 상한과 `availability_basis: date_upper_bound`를 사용합니다. 정확한 시각을 확인했으면 `verified_exact_timestamp`를 지정합니다.
- 신규 후보 실행의 `--qualitative-registry`는 [기본 출처 설정](discovery_sources.json)을 교체합니다. `--replay-screen <evidence.json>`과 `--auto-qualitative`를 함께 쓰면 선별을 재사용하고 정성 출처를 다시 조회합니다.

시장 가격은 배당이 포함되지 않은 수익률을 사용합니다. 독립 기사와 기업 발표는 공개 시점과 출처를 점검하고, 해당 종목에 허용된 근거를 모델에 전달합니다. 기사 본문에 접근하지 못하면 이 상태를 실행 기록에 남깁니다.


## 뉴스 원문을 기초 분석에 반영하는 재평가

v0.1에서 검증된 개선 경로는 저장된 통합 결과를 대상으로 합니다. 일반 `integrated --holdings`의 새 자료 분석과 구별됩니다. 이 경로는 가격·공시 기준과 보유 목록·기존 최종 후보(최대 4개)를 유지하고, 별도 공개 기준까지의 뉴스만 기초 역할 분석 전에 추가합니다. 새 40기업 예비심사와 후보 재선정은 하지 않습니다. 후보가 없으면 후보 모델 호출도 없습니다.

```sh
python -B -m research_prototype.news_context --baseline runs/demo/evidence.json --output runs/news-context --max-requests 64
python -B -m research_prototype.news_reanalysis --baseline runs/demo/evidence.json --news-bundle runs/news-context/news-context.json --output-dir runs/news-prepared --collect-only
python -B -m research_prototype.news_reanalysis --baseline runs/demo/evidence.json --news-bundle runs/news-context/news-context.json --output-dir runs/news-analysis --monitor-dir runs/news-monitor --model gpt-6.1-sol --timeout 600 --workers 3 --pdf
```

첫 명령은 계정·키 없는 공개 RSS와 기사 본문을 요청합니다. 기본 대상은 저장된 보유와 최종 후보의 합집합이며 `--tickers`로 제한할 수 있습니다. 과거 기준 뒤에 수정됐거나 과거 본문을 입증하지 못한 기사는 제목 단서 또는 미확인 상태로 남습니다. `verified_body`는 원문 추출 확인이며 기사 주장의 사실 검증이 아닙니다. `--cutoff`를 따로 지정한다면 분석에 사용하는 저장본의 공개 기준과 정확히 일치해야 합니다.

`news_reanalysis --collect-only`는 전달된 저장 자료만 결합·검증하며 추가 수집과 모델 호출을 하지 않습니다. 실제 재평가는 기존 로그인된 Codex CLI를 사용합니다. 보유별 역할과 독립 검토·조건부 추가 분석을 실행하며, 필수 가격이 없는 종목은 거래 판단을 보류한 채 뉴스 설명만 별도로 만들 수 있습니다. 원출력·프롬프트·오류·사용량은 JSON과 모니터에 남고 투자 보고서에는 사용량 표를 넣지 않습니다.

실패하면 같은 baseline/bundle에 `--resume <실패 evidence.json 또는 checkpoint>`와 **새 output-dir 및 monitor-dir**을 지정합니다. 성공한 종목과 단계는 건너뛰며, 실패한 종목의 성공 역할도 입력·프롬프트가 정확히 같을 때만 재사용합니다. 원출력을 자동 수정하거나 검증 실패를 성공으로 간주하지 않습니다. 모니터는 본 패키지의 `research_prototype.monitoring`을 사용하므로 임시 실험 폴더가 필요 없습니다. 일반 새 자료 통합 실행을 기록할 때도 `python -B -m research_prototype.monitoring --monitor-dir runs/live-monitor --holdings research_prototype/examples/usa.csv --output-dir runs/live`를 사용할 수 있습니다.

검증된 실제 비교 범위는 미국·한국 보유 9종과 신규 후보 4개였으며, 일반 후보 수 0~4개와 다른 티커의 범위 구성은 오프라인 회귀로 점검했습니다. 모든 상품·뉴스 제공처에서 본문을 확보할 수 있다는 뜻은 아닙니다. 상세 비교·사용량 보고서는 요청할 때만 별도 `news_comparison` 또는 `runtime_report`로 만듭니다. 저장 데이터와 보고서는 저장소에 포함하지 않습니다.
