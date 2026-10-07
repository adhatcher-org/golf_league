// Basic JavaScript for Golf League application

document.addEventListener('DOMContentLoaded', function() {
    // Application initialization code
    console.log('Golf League application loaded');

    const contactDialog = document.getElementById('opponent-contact-dialog');
    if (contactDialog && typeof contactDialog.showModal === 'function') {
        const contactName = document.getElementById('opponent-contact-name');
        const contactWeek = document.getElementById('opponent-contact-week');
        const contactEmail = document.getElementById('opponent-contact-email');
        const noEmail = document.getElementById('opponent-contact-no-email');
        const contactPhone = document.getElementById('opponent-contact-phone');
        const noPhone = document.getElementById('opponent-contact-no-phone');
        const contactCall = document.getElementById('opponent-contact-call');
        const contactText = document.getElementById('opponent-contact-text');
        const contactStatus = document.getElementById('opponent-contact-status');

        document.querySelectorAll('[data-opponent-contact]').forEach((link) => {
            link.addEventListener('click', async (event) => {
                event.preventDefault();
                contactDialog.showModal();
                contactName.textContent = 'Loading contact details…';
                contactWeek.textContent = '';
                contactStatus.textContent = '';
                contactEmail.hidden = true;
                noEmail.hidden = true;
                contactPhone.hidden = true;
                contactCall.hidden = true;
                noPhone.hidden = true;
                contactText.hidden = true;

                try {
                    const response = await fetch(link.href, {
                        headers: { Accept: 'application/json' },
                        credentials: 'same-origin',
                    });
                    if (!response.ok) throw new Error('Contact details unavailable');
                    const details = await response.json();

                    contactName.textContent = details.name;
                    contactWeek.textContent = `Week ${details.week_index} · ${details.week_date}`;
                    if (details.email) {
                        contactEmail.textContent = details.email;
                        contactEmail.href = `mailto:${details.email}`;
                        contactEmail.hidden = false;
                    } else {
                        noEmail.hidden = false;
                    }
                    if (details.phone) {
                        const dialable = details.phone.replace(/[^0-9+*#,;]/g, '');
                        contactPhone.textContent = details.phone;
                        contactPhone.href = `tel:${dialable}`;
                        contactPhone.hidden = false;
                        if (dialable) {
                            contactCall.href = `tel:${dialable}`;
                            contactCall.hidden = false;
                            contactText.href = `sms:${dialable}`;
                            contactText.hidden = false;
                        }
                    } else {
                        noPhone.hidden = false;
                    }
                } catch (error) {
                    contactName.textContent = 'Contact details unavailable';
                    contactStatus.textContent = 'Contact details could not be loaded. Open the contact page: ';
                    const fallback = document.createElement('a');
                    fallback.href = link.href;
                    fallback.textContent = 'Open contact page';
                    contactStatus.append(fallback);
                }
            });
        });

        contactDialog.addEventListener('click', (event) => {
            if (event.target === contactDialog) contactDialog.close();
        });
    }

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
