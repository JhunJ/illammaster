/**
 * 일람 추출 우측 탭(보 / 기둥 / 벽체 / 슬라브) 공통 정의.
 * index.html 인라인 스크립트보다 먼저 로드되어야 하며, 전역 IllamExtractCatalog 를 채운다.
 *
 * 이 파일을 수정하면 index.html 의 동일 폴백 블록(스크립트 직후 주석 “동기화”)도 같이 맞출 것.
 */
(function (global) {
  'use strict';

  /** API category 문자열 — 서버 extract category 와 동일해야 함 */
  var KEYS = ['beam', 'column', 'wall', 'slab'];

  /** 우측 패널 루트 div id (index.html 의 extract-tabpanel 과 대응) */
  var PANEL_IDS = {
    beam: 'rpBeam',
    column: 'rpColumn',
    wall: 'rpWall',
    slab: 'rpSlab',
  };

  function emptyRunsRecord() {
    var o = {};
    KEYS.forEach(function (k) {
      o[k] = null;
    });
    return o;
  }

  global.IllamExtractCatalog = {
    keys: KEYS.slice(),
    panelIds: Object.assign({}, PANEL_IDS),

    isValidKey: function (k) {
      return KEYS.indexOf(String(k || '')) >= 0;
    },

    panelIdFor: function (k) {
      return PANEL_IDS[k] || null;
    },

    /** 표 영역(selection bbox) UI·API 공통 — 기둥·보 */
    usesScheduleSelectionBBox: function (k) {
      return k === 'beam' || k === 'column';
    },

    forEachKey: function (fn) {
      KEYS.forEach(fn);
    },

    cloneRunsMap: function (lastRunByCategory, deepCloneJson) {
      var out = emptyRunsRecord();
      KEYS.forEach(function (k) {
        out[k] = deepCloneJson(lastRunByCategory && lastRunByCategory[k]);
      });
      return out;
    },

    applyRunsMap: function (lastRunByCategory, snapRuns, deepCloneJson) {
      KEYS.forEach(function (k) {
        lastRunByCategory[k] = deepCloneJson(snapRuns && snapRuns[k]);
      });
    },

    clearRunsMap: function (lastRunByCategory) {
      KEYS.forEach(function (k) {
        lastRunByCategory[k] = null;
      });
    },

    emptyRunsRecord: emptyRunsRecord,
  };
})(typeof window !== 'undefined' ? window : this);
