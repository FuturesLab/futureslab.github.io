(function() {
    'use strict';

    // Determine base path based on current page location
    function getBasePath() {
        const path = window.location.pathname;
        const depth = (path.match(/\//g) || []).length - 1;
        
        // Check if we're in a subdirectory
        if (path.includes('/bugs/') || path.includes('/papers/')) {
            return '../';
        }
        
        // For root-level pages
        return './';
    }

    const basePath = getBasePath();

    // Replace {{BASE_PATH}} placeholder in HTML content
    function replaceBasePath(html) {
        return html.replace(/\{\{BASE_PATH\}\}/g, basePath);
    }

    // Load an HTML include file
    async function loadInclude(url, targetSelector, position = 'replace') {
        try {
            const response = await fetch(basePath + url);
            if (!response.ok) {
                throw new Error(`Failed to load ${url}: ${response.status}`);
            }
            const html = await response.text();
            const processedHtml = replaceBasePath(html);
            
            const target = document.querySelector(targetSelector);
            if (target) {
                if (position === 'replace') {
                    target.innerHTML = processedHtml;
                } else if (position === 'prepend') {
                    target.insertAdjacentHTML('afterbegin', processedHtml);
                } else if (position === 'append') {
                    target.insertAdjacentHTML('beforeend', processedHtml);
                }
            }
            return processedHtml;
        } catch (error) {
            console.error('Error loading include:', error);
            return null;
        }
    }

    // Initialize accessibility features after navbar loads
    function initAccessibility() {
        // Skip link focus management
        const skipLink = document.querySelector('.skip-link');
        const mainContent = document.getElementById('main-content');
        
        if (skipLink && mainContent) {
            skipLink.addEventListener('click', function(e) {
                e.preventDefault();
                mainContent.setAttribute('tabindex', '-1');
                mainContent.focus();
                mainContent.removeAttribute('tabindex');
            });
        }

        // Mobile toggle aria-expanded management
        const navbarToggle = document.querySelector('.navbar-toggle');
        const navbarCollapse = document.getElementById('myNavbar');
        
        if (navbarToggle && navbarCollapse) {
            // Update aria-expanded on toggle
            navbarToggle.addEventListener('click', function() {
                const isExpanded = navbarCollapse.classList.contains('in');
                navbarToggle.setAttribute('aria-expanded', !isExpanded);
            });

            // Bootstrap 3 collapse events
            $(navbarCollapse).on('shown.bs.collapse', function() {
                navbarToggle.setAttribute('aria-expanded', 'true');
            });

            $(navbarCollapse).on('hidden.bs.collapse', function() {
                navbarToggle.setAttribute('aria-expanded', 'false');
            });
        }
    }

    // Load all includes when DOM is ready
    document.addEventListener('DOMContentLoaded', async function() {
        // Load head content into a placeholder if it exists
        const headPlaceholder = document.getElementById('head-includes');
        if (headPlaceholder) {
            const headContent = await loadInclude('includes/head.html', '#head-includes', 'replace');
            if (headContent) {
                // Move content to actual head
                const tempDiv = document.createElement('div');
                tempDiv.innerHTML = headContent;
                const headElement = document.head;
                Array.from(tempDiv.children).forEach(child => {
                    headElement.appendChild(child.cloneNode(true));
                });
            }
        }

        // Load navbar
        const navbarPlaceholder = document.getElementById('navbar-placeholder');
        if (navbarPlaceholder) {
            await loadInclude('includes/navbar.html', '#navbar-placeholder', 'replace');
            initAccessibility();
        }

        // Load footer
        const footerPlaceholder = document.getElementById('footer-placeholder');
        if (footerPlaceholder) {
            await loadInclude('includes/footer.html', '#footer-placeholder', 'replace');
        }
    });
})();
