// Basic JavaScript for Golf League application

document.addEventListener('DOMContentLoaded', function() {
    // Application initialization code
    console.log('Golf League application loaded');

    const seasonRoster = document.getElementById('season-roster-form');
    if (seasonRoster) {
        const golfers = Array.from(seasonRoster.querySelectorAll('.season-golfer-checkbox'));
        const selectAll = document.getElementById('select-all-golfers');
        const count = document.getElementById('selected-golfer-count');
        // Keep the rendered alphabetical order even after rows have moved.
        const rows = golfers.map((checkbox, index) => ({
            checkbox,
            row: checkbox.closest('tr'),
            index,
        })).filter((item) => item.row);

        if (!selectAll || !count) return;

        const reorderRows = function() {
            const focusedControl = document.activeElement;
            const orderedRows = rows.slice().sort((left, right) =>
                Number(right.checkbox.checked) - Number(left.checkbox.checked)
                || left.index - right.index
            );
            orderedRows.forEach((item, index) => {
                item.row.classList.toggle('is-selected', item.checkbox.checked);
                const parent = item.row.parentElement;
                if (parent.children[index] !== item.row) {
                    parent.insertBefore(item.row, parent.children[index] || null);
                }
            });
            // Moving an existing row can blur its checkbox in some browsers.
            if (seasonRoster.contains(focusedControl)
                    && document.activeElement !== focusedControl) {
                focusedControl.focus({ preventScroll: true });
            }
        };

        const updateSelection = function() {
            const activeGolfers = golfers.filter((checkbox) => !checkbox.disabled);
            const selected = golfers.filter((checkbox) =>
                checkbox.checked && checkbox.dataset.active === 'true'
            ).length;
            count.textContent = String(selected);
            selectAll.checked = activeGolfers.length > 0 && activeGolfers.every((checkbox) => checkbox.checked);
            selectAll.indeterminate = activeGolfers.some((checkbox) => checkbox.checked)
                && !selectAll.checked;
            reorderRows();
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
