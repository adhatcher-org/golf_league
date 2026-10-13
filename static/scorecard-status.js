// Scorecard status page: ask every two seconds whether the photo has been read,
// and reload when it has. Without JavaScript the page's refresh link does the same.
(function () {
    var marker = document.querySelector('[data-scorecard-status-url]');
    if (!marker || typeof window.fetch !== 'function') return;
    var url = marker.getAttribute('data-scorecard-status-url');
    var again = function () { window.setTimeout(check, 2000); };

    function check() {
        window.fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
            .then(function (response) {
                if (response.status === 404) return { status: 'gone' };
                return response.ok ? response.json() : null;
            })
            .then(function (state) {
                if (state && state.status !== 'extracting') window.location.reload();
                else again();
            })
            .catch(again);
    }

    again();
}());
