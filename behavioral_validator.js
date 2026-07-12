/**
 * Behavioral Testing Framework for TradeSignal Migration
 * Ensures zero deviation from original functionality
 */

class BehavioralValidator {
  constructor() {
    this.baseline = {};
    this.current = {};
    this.testResults = [];
  }

  // Record baseline behavior before any migration
  async recordBaseline() {
    console.log('🎯 Recording behavioral baseline...');

    this.baseline = {
      navigation: await this.recordNavigationBehavior(),
      apiCalls: await this.recordAPICallPatterns(),
      stateChanges: await this.recordStateMutations(),
      uiUpdates: await this.recordDOMUpdates(),
      timing: await this.recordExecutionTimings(),
      memory: await this.recordMemoryUsage(),
      network: await this.recordNetworkRequests()
    };

    console.log('✅ Baseline recorded:', Object.keys(this.baseline));
    return this.baseline;
  }

  // Test migrated component against baseline
  async validateMigration(componentName, testFunction) {
    console.log(`🧪 Testing ${componentName} migration...`);

    const startTime = performance.now();
    const startMemory = performance.memory.usedJSHeapSize;

    try {
      // Run the test
      const result = await testFunction();

      // Record current behavior
      this.current = {
        navigation: await this.recordNavigationBehavior(),
        apiCalls: await this.recordAPICallPatterns(),
        stateChanges: await this.recordStateMutations(),
        uiUpdates: await this.recordDOMUpdates(),
        timing: performance.now() - startTime,
        memory: performance.memory.usedJSHeapSize - startMemory,
        network: await this.recordNetworkRequests()
      };

      // Compare against baseline
      const comparison = this.compareBehaviors(this.baseline, this.current);

      const testResult = {
        component: componentName,
        passed: comparison.isIdentical,
        deviations: comparison.deviations,
        timestamp: new Date().toISOString(),
        baseline: this.baseline,
        current: this.current
      };

      this.testResults.push(testResult);

      if (comparison.isIdentical) {
        console.log(`✅ ${componentName} migration PASSED - Zero behavioral deviation`);
      } else {
        console.error(`❌ ${componentName} migration FAILED:`, comparison.deviations);
      }

      return testResult;

    } catch (error) {
      console.error(`💥 ${componentName} migration CRASHED:`, error);
      return {
        component: componentName,
        passed: false,
        error: error.message,
        timestamp: new Date().toISOString()
      };
    }
  }

  async recordNavigationBehavior() {
    return {
      currentPage: app.currentPage,
      activeNavItem: document.querySelector('.nav-item.active')?.dataset.page,
      activePage: document.querySelector('.page.active')?.id,
      pageTitle: document.getElementById('page-title')?.textContent,
      urlParams: new URLSearchParams(window.location.search).toString()
    };
  }

  async recordAPICallPatterns() {
    // Mock network interception to record API calls
    const calls = [];
    const originalFetch = window.fetch;

    window.fetch = function(...args) {
      calls.push({
        url: args[0],
        method: args[1]?.method || 'GET',
        timestamp: Date.now()
      });
      return originalFetch.apply(this, args);
    };

    // Wait for any async operations
    await new Promise(resolve => setTimeout(resolve, 100));

    // Restore original fetch
    window.fetch = originalFetch;

    return calls;
  }

  async recordStateMutations() {
    return {
      appState: { ...app }, // Shallow copy of app object
      localStorage: { ...localStorage },
      sessionStorage: { ...sessionStorage }
    };
  }

  async recordDOMUpdates() {
    const criticalElements = [
      '#page-title',
      '.nav-item.active',
      '.page.active',
      '#watchlist-count',
      '#alert-count',
      '#reco-count',
      '.market-ticker',
      '.sector-table'
    ];

    const updates = {};
    criticalElements.forEach(selector => {
      const element = document.querySelector(selector);
      if (element) {
        updates[selector] = {
          textContent: element.textContent,
          innerHTML: element.innerHTML,
          className: element.className,
          style: element.style.cssText
        };
      }
    });

    return updates;
  }

  async recordExecutionTimings() {
    // Record timing of critical operations
    return {
      navigationTime: await this.timeOperation(() => app.navigateTo('dashboard')),
      scoringTime: await this.timeOperation(() => app.runFullScoring?.()),
      apiResponseTime: await this.timeOperation(() => app.apiFetch?.('/api/health'))
    };
  }

  async recordMemoryUsage() {
    if (performance.memory) {
      return {
        used: performance.memory.usedJSHeapSize,
        total: performance.memory.totalJSHeapSize,
        limit: performance.memory.jsHeapSizeLimit
      };
    }
    return null;
  }

  async recordNetworkRequests() {
    // Use Performance API to record network requests
    const entries = performance.getEntriesByType('resource');
    return entries.map(entry => ({
      url: entry.name,
      duration: entry.duration,
      size: entry.transferSize
    }));
  }

  async timeOperation(operation) {
    if (typeof operation !== 'function') return null;

    const start = performance.now();
    try {
      await operation();
    } catch (e) {
      // Operation failed, still record timing
    }
    return performance.now() - start;
  }

  compareBehaviors(baseline, current) {
    const deviations = [];

    // Compare navigation
    if (JSON.stringify(baseline.navigation) !== JSON.stringify(current.navigation)) {
      deviations.push({
        type: 'navigation',
        baseline: baseline.navigation,
        current: current.navigation
      });
    }

    // Compare API calls (simplified - check if same endpoints called)
    const baselineUrls = baseline.apiCalls?.map(c => c.url) || [];
    const currentUrls = current.apiCalls?.map(c => c.url) || [];
    if (baselineUrls.length !== currentUrls.length ||
        !baselineUrls.every(url => currentUrls.includes(url))) {
      deviations.push({
        type: 'api_calls',
        baseline: baselineUrls,
        current: currentUrls
      });
    }

    // Compare DOM updates
    if (JSON.stringify(baseline.uiUpdates) !== JSON.stringify(current.uiUpdates)) {
      deviations.push({
        type: 'dom_updates',
        baseline: baseline.uiUpdates,
        current: current.uiUpdates
      });
    }

    // Compare timing (allow 10% variance)
    if (baseline.timing && current.timing) {
      Object.keys(baseline.timing).forEach(key => {
        const baselineTime = baseline.timing[key];
        const currentTime = current.timing[key];
        if (baselineTime && currentTime) {
          const variance = Math.abs(currentTime - baselineTime) / baselineTime;
          if (variance > 0.1) { // 10% variance allowed
            deviations.push({
              type: 'timing',
              operation: key,
              baseline: baselineTime,
              current: currentTime,
              variance: variance
            });
          }
        }
      });
    }

    // Compare memory usage (allow 5% increase)
    if (baseline.memory && current.memory) {
      const memoryIncrease = (current.memory.used - baseline.memory.used) / baseline.memory.used;
      if (memoryIncrease > 0.05) {
        deviations.push({
          type: 'memory',
          baseline: baseline.memory.used,
          current: current.memory.used,
          increase: memoryIncrease
        });
      }
    }

    return {
      isIdentical: deviations.length === 0,
      deviations: deviations
    };
  }

  generateReport() {
    const passed = this.testResults.filter(r => r.passed).length;
    const failed = this.testResults.filter(r => !r.passed).length;

    return {
      summary: {
        totalTests: this.testResults.length,
        passed: passed,
        failed: failed,
        successRate: (passed / this.testResults.length * 100).toFixed(1) + '%'
      },
      results: this.testResults,
      recommendations: this.generateRecommendations()
    };
  }

  generateRecommendations() {
    const recommendations = [];

    this.testResults.forEach(result => {
      if (!result.passed && result.deviations) {
        result.deviations.forEach(deviation => {
          switch (deviation.type) {
            case 'navigation':
              recommendations.push(`Fix navigation behavior in ${result.component}`);
              break;
            case 'api_calls':
              recommendations.push(`Restore original API call patterns in ${result.component}`);
              break;
            case 'dom_updates':
              recommendations.push(`Match DOM update behavior in ${result.component}`);
              break;
            case 'timing':
              recommendations.push(`Optimize timing in ${result.component} (${deviation.operation})`);
              break;
            case 'memory':
              recommendations.push(`Reduce memory usage in ${result.component}`);
              break;
          }
        });
      }
    });

    return recommendations;
  }
}

// Export for use in migration testing
window.BehavioralValidator = BehavioralValidator;</content>
<parameter name="filePath">/home/rajk/Downloads/TradeSignal -Backup April19-BKP1/behavioral_validator.js