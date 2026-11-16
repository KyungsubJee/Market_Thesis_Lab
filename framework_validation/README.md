# 분석 프레임워크 분기 검증

이 폴더는 테스트 전용이야. 오프라인 실행기, 실제 모델 검증기와 고정 자료를 담고 있어. Python 3.11 이상과 표준 라이브러리를 사용해.

저장소 루트에서 실행해.

```powershell
python -B -m framework_validation.runner all
python -B -m unittest discover -s framework_validation/tests -v
```

`all` 대신 `coca_cola`, `samsung`, `meta`를 지정하면 해당 사례만 실행돼. 실행기는 사례별 JSON을 표준 출력에 남겨. `path`는 실행 경로, `trace`는 역할별 호출 순서와 근거 ID, `status`는 검토 상태, `missing_fields`는 자료 공백이야.

| 고정 자료 | 모의 검토 내용 | 예상 경로 |
| --- | --- | --- |
| 코카콜라 2024년 2분기 | 추가로 다룰 중대한 쟁점 없음 | `basic` |
| 삼성전자 2024년 3분기 | 경쟁사 HBM 성장과 삼성전자 이익 전망을 연결하는 가정 확인 | `secondary` |
| 메타 2024년 1분기 | 같은 중기 기준에서 AI 설비투자와 현금흐름 부담을 두고 이견 | `debate` |

각 자료의 `evidence`에는 공식 실적 근거와 사용 가능 시각이 들어 있어. `mock_reviews`에는 어떤 쟁점을 제기하고 재검토할지 미리 지정해 두었어. `complete`는 **모의 흐름이 완료됐다는 뜻**이야. 실제 수익률 전망, LLM의 판단 능력, 데이터 공급자 연동이나 운영 보고서 품질은 이 시험에서 평가하지 않아.

## 실제 모델 최적화 검증

ChatGPT로 로그인한 Codex CLI가 필요해. 사례별로 새 결과 파일을 지정해.

```powershell
python -B -m framework_validation.optimized_runner meta --model gpt-6.1-sol --output framework_validation/results/meta-new-run.json
```

실행기는 분석 기준 시각까지의 근거를 역할별로 전달해. 독립 검토 후 결론을 바꿀 수 있는 쟁점에만 수정, 해당 분야 2차 분석 또는 찬반 검토를 적용해. 찬반 검토는 양쪽 독립 주장 한 건씩 받고, 재검토 후에도 남은 쟁점은 결과에 표시해. 고정 자료의 `mock_reviews`는 실제 모델 입력에서 제외돼.

결과에는 입력 스냅샷 해시, 호출 경로와 쟁점, 실제 최종 종합, 호출별 소요 시간·토큰·프롬프트 문자 수가 들어 있어.

`--reuse-analysis`에 같은 모델과 입력 스냅샷의 이전 결과를 전달하면 거시·섹터·가격·이벤트 분석을 재사용하고 펀더멘털부터 다시 실행해. 재실행 비용은 최상위 호출·토큰 항목에, 이전 호출까지 합친 비교용 수치는 `effective_run`에 들어 있어.
