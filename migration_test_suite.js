/**
 * Migration Testing Suite
 * Tests each component migration against behavioral baseline
 */

const validator = new BehavioralValidator();

// Test scenarios for each component
const migrationTests = {

  async testNavigationMigration() {
    return validator.validateMigration('Navigation', async () => {
      // Test all navigation paths
      const pages = [
        'dashboard', 'screener', 'analysis',
        'watchlist', 'portfolio', 'live-movers', 'index-movers', 'news',
        'strategy', 'backtest', 'journal', 'paper', 'recommendations',
        'reco-tracker', 'historical', 'alerts', 'settings',
        'smc-dashboard', 'multi-chart', 'fno-session'
      ];

      for (const page of pages) {
        app.navigateTo(page);
        await new Promise(resolve => setTimeout(resolve, 50)); // Allow DOM updates
      }
    });
  },

  async testDashboardMigration() {
    return validator.validateMigration('Dashboard', async () => {
      app.navigateTo('dashboard');
      await app.bindDashboard?.();

      // Wait for data loading
      await new Promise(resolve => setTimeout(resolve, 1000));

      // Test market breadth loading
      await app.apiFetch?.('/api/market-breadth');

      // Test sector sorting
      app.sectorSortField = 'avgScore';
      app.runSectorSort?.();
    });
  },

  async testScreenerMigration() {
    return validator.validateMigration('Equity Screener', async () => {
      app.navigateTo('screener');
      await app.bindScreener?.();

      // Test scoring run (this will take time)
      const scoringPromise = app.runFullScoring?.();

      // Monitor progress
      let progress = 0;
      const progressInterval = setInterval(() => {
        const progressEl = document.getElementById('scoring-progress');
        if (progressEl) {
          console.log('Scoring progress:', progressEl.textContent);
        }
      }, 1000);

      await scoringPromise;
      clearInterval(progressInterval);

      // Test sorting
      app.sectorSortField = 'avgScore';
      app.sectorSortDesc = true;
      app.updateSectorDisplay?.();
    });
  },


  async testWatchlistMigration() {
    return validator.validateMigration('Watchlist', async () => {
      app.navigateTo('watchlist');
      await app.bindWatchlist?.();

      // Test adding stocks to watchlist
      app.addToWatchlist?.('RELIANCE');
      app.addToWatchlist?.('TCS');

      // Test removing stocks
      app.removeFromWatchlist?.('RELIANCE');

      // Check badge count update
      const badge = document.getElementById('watchlist-count');
      console.log('Watchlist count:', badge?.textContent);
    });
  },

  async testSettingsMigration() {
    return validator.validateMigration('Settings', async () => {
      app.navigateTo('settings');
      await app.bindSettings?.();

      // Test config loading
      await app.loadConfig?.();

      // Test form validation
      const apiKeyInput = document.getElementById('set-api-key');
      if (apiKeyInput) {
        apiKeyInput.value = 'test_api_key';
        apiKeyInput.dispatchEvent(new Event('input'));
      }
    });
  },

  async testHistoricalChartsMigration() {
    return validator.validateMigration('Historical Charts', async () => {
      app.navigateTo('historical');

      // Test chart initialization
      if (!chartManager.chart) {
        chartManager.init('main-chart');
      }

      // Test symbol input
      const symbolInput = document.getElementById('historical-symbol-input');
      if (symbolInput) {
        symbolInput.value = 'RELIANCE';
        symbolInput.dispatchEvent(new Event('change'));
      }

      // Wait for chart loading
      await new Promise(resolve => setTimeout(resolve, 2000));
    });
  }

};

// Automated test runner
async function runMigrationTests() {
  console.log('🚀 Starting Migration Behavioral Tests...');

  // Record baseline first
  await validator.recordBaseline();

  const results = [];

  // Run all component tests
  for (const [testName, testFunction] of Object.entries(migrationTests)) {
    try {
      const result = await testFunction();
      results.push(result);

      if (!result.passed) {
        console.error(`❌ ${testName} FAILED - Blocking migration`);
        break; // Stop on first failure
      }
    } catch (error) {
      console.error(`💥 ${testName} CRASHED:`, error);
      results.push({
        component: testName,
        passed: false,
        error: error.message
      });
      break;
    }
  }

  // Generate final report
  const report = validator.generateReport();

  console.log('📊 Migration Test Report:');
  console.log(`Total Tests: ${report.summary.totalTests}`);
  console.log(`Passed: ${report.summary.passed}`);
  console.log(`Failed: ${report.summary.failed}`);
  console.log(`Success Rate: ${report.summary.successRate}`);

  if (report.recommendations.length > 0) {
    console.log('🔧 Recommendations:');
    report.recommendations.forEach(rec => console.log(`- ${rec}`));
  }

  // Save report
  const reportBlob = new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(reportBlob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `migration_test_report_${new Date().toISOString()}.json`;
  a.click();

  return report;
}

// Make available globally
window.runMigrationTests = runMigrationTests;
window.migrationTests = migrationTests;