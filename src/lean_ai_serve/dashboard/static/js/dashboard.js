/* lean-ai-serve dashboard JavaScript */

// Show toast on HTMX errors
document.addEventListener('htmx:responseError', function(event) {
    window.dispatchEvent(new CustomEvent('show-toast', {
        detail: { message: 'Request failed: ' + (event.detail.xhr.statusText || 'Unknown error'), type: 'error' }
    }));
});

// Show toast on successful state-changing operations
document.addEventListener('htmx:afterRequest', function(event) {
    var method = (event.detail.requestConfig && event.detail.requestConfig.verb) || '';
    if (method.toUpperCase() === 'POST' || method.toUpperCase() === 'DELETE') {
        if (event.detail.successful) {
            window.dispatchEvent(new CustomEvent('show-toast', {
                detail: { message: 'Action completed successfully', type: 'success' }
            }));
        }
    }
});

// Re-initialize any Chart.js canvases after HTMX swaps
document.addEventListener('htmx:afterSwap', function(event) {
    initCharts(event.target);
});

// Chart.js initialization from data attributes
function initCharts(root) {
    if (typeof Chart === 'undefined') return;

    var canvases = (root || document).querySelectorAll('canvas[data-chart]');
    canvases.forEach(function(canvas) {
        // Destroy existing chart if any
        var existing = Chart.getChart(canvas);
        if (existing) existing.destroy();

        try {
            var config = JSON.parse(canvas.getAttribute('data-chart'));
            new Chart(canvas, config);
        } catch (e) {
            console.warn('Failed to initialize chart:', e);
        }
    });
}

// Initialize charts on page load
document.addEventListener('DOMContentLoaded', function() {
    initCharts();
});
