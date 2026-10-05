// Basic JavaScript for Golf League application

document.addEventListener('DOMContentLoaded', function() {
    // Application initialization code
    console.log('Golf League application loaded');

    const seasonRoster = document.getElementById('season-roster-form');
    if (seasonRoster) {
        const golfers = Array.from(seasonRoster.querySelectorAll('.season-golfer-checkbox'));
        const selectAll = document.getElementById('select-all-golfers');
        const count = document.getElementById('selected-golfer-count');

        if (!selectAll || !count) return;

        const updateSelection = function() {
            const activeGolfers = golfers.filter((checkbox) => !checkbox.disabled);
            const selected = golfers.filter((checkbox) =>
                checkbox.checked && checkbox.dataset.active === 'true'
            ).length;
            count.textContent = String(selected);
            selectAll.checked = activeGolfers.length > 0 && activeGolfers.every((checkbox) => checkbox.checked);
            selectAll.indeterminate = activeGolfers.some((checkbox) => checkbox.checked)
                && !selectAll.checked;
        };

        golfers.forEach((checkbox) => checkbox.addEventListener('change', updateSelection));
        selectAll.addEventListener('change', function() {
            golfers.forEach((checkbox) => {
                if (!checkbox.disabled) checkbox.checked = selectAll.checked;
            });
            updateSelection();
        });
        updateSelection();
    }
});
