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
        let contactRequest = 0;

        const clearContactLinks = () => {
            [contactEmail, contactPhone, contactCall, contactText].forEach((anchor) => {
                anchor.hidden = true;
                anchor.removeAttribute('href');
            });
            contactEmail.textContent = '';
            contactPhone.textContent = '';
        };

        // A closed dialog must never be updated by an earlier request.
        contactDialog.addEventListener('close', () => {
            // Native close events may arrive after another opponent was opened.
            if (!contactDialog.open) {
                contactRequest += 1;
                clearContactLinks();
            }
        });

        document.querySelectorAll('[data-opponent-contact]').forEach((link) => {
            link.addEventListener('click', async (event) => {
                event.preventDefault();
                const request = ++contactRequest;
                contactDialog.showModal();
                contactName.textContent = 'Loading contact details…';
                contactWeek.textContent = '';
                contactStatus.textContent = '';
                clearContactLinks();
                noEmail.hidden = true;
                noPhone.hidden = true;

                try {
                    const response = await fetch(link.href, {
                        headers: { Accept: 'application/json' },
                        credentials: 'same-origin',
                    });
                    if (!response.ok) throw new Error('Contact details unavailable');
                    const details = await response.json();
                    if (request !== contactRequest || !contactDialog.open) return;

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
                    if (request !== contactRequest || !contactDialog.open) return;
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

    // Suggest only the next golfer, without replacing a choice or repeating a player.
    document.querySelectorAll('.scorecard-form').forEach((form) => {
        const golfers = Array.from(form.querySelectorAll('.scorecard-player select[name^="golfer_"]'));
        let fillingOpponent = false;
        golfers.forEach((select, index) => {
            select.addEventListener('change', () => {
                if (fillingOpponent) return;
                const opponent = select.selectedOptions[0]?.dataset.opponent;
                const next = golfers[index + 1];
                if (!opponent || !next || next.value
                        || golfers.some((golfer) => golfer.value === opponent)
                        || !Array.from(next.options).some((option) => option.value === opponent)) return;
                next.value = opponent;
                // Notify other listeners, but do not suggest the original golfer again.
                fillingOpponent = true;
                try {
                    next.dispatchEvent(new Event('change', { bubbles: true }));
                } finally {
                    fillingOpponent = false;
                }
            });
        });
    });

    // Dynamically total each score row as hole scores are entered.
    // Runs on every page via base.html; score-input rows exist only on score pages.
    function scoreRowTotal(row) {
        const inputs = row.querySelectorAll('.score-input');
        const totalCell = row.querySelector('.score-total');
        if (!totalCell) return;
        let total = 0;
        let any = false;
        inputs.forEach((cell) => {
            const value = parseInt(cell.value, 10);
            if (!isNaN(value)) {
                total += value;
                any = true;
            }
        });
        totalCell.textContent = any ? String(total) : '—';
        const handicapCell = row.querySelector('.score-handicap');
        const netCell = row.querySelector('.score-net');
        const handicapRaw = row.dataset.handicap || "";
        const handicap = handicapRaw === "" ? null : parseInt(handicapRaw, 10);
        if (handicapCell) {
            handicapCell.textContent = handicap === null ? '—' : handicapRaw;
        }
        if (netCell) {
            netCell.textContent = (any && handicap !== null) ? String(total - handicap) : '—';
        }
    }

    document.querySelectorAll('.score-input').forEach((input) => {
        const row = input.closest('tr');
        if (!row) return;
        input.addEventListener('input', () => scoreRowTotal(row));
    });
    document.querySelectorAll('.score-total').forEach((cell) => {
        const row = cell.closest('tr');
        if (row) scoreRowTotal(row);
    });

    // Collapse the hole-detail columns of every score grid on the page, leaving
    // only Golfer, Gross, HC and Net. Toggled from the scores page toolbar.
    const collapseToggle = document.getElementById('collapse-toggle');
    const scoreTables = document.getElementById('score-tables');
    if (collapseToggle && scoreTables) {
        const holeCells = scoreTables.querySelectorAll('.score-hole');
        const setCollapsed = (collapsed) => {
            scoreTables.classList.toggle('is-collapsed', collapsed);
            collapseToggle.setAttribute('aria-pressed', String(collapsed));
            collapseToggle.textContent = collapsed ? 'Show hole details' : 'Collapse hole details';
        };
        collapseToggle.addEventListener('click', () =>
            setCollapsed(!scoreTables.classList.contains('is-collapsed')));
        setCollapsed(false);
    }

    // Roster page: golfer detail dialog (contact fetched on demand), plus
    // client-side column sort and text filter. Guards on element ids so this
    // only runs on /roster.
    const rosterBody = document.getElementById('roster-body');
    const rosterDialog = document.getElementById('roster-detail-dialog');
    if (!rosterBody || !rosterDialog) return;

    const rosterFilter = document.getElementById('roster-filter');
    const detailName = document.getElementById('roster-detail-name');
    const detailFields = document.getElementById('roster-detail-fields');
    const detailStatus = document.getElementById('roster-detail-status');

    let sortKey = null;
    let sortDir = 1; // 1 = ascending, -1 = descending

    const cellSortValue = (row, key) => {
        const cell = row.querySelector('[data-sort-cell="' + key + '"]');
        return cell ? (cell.dataset.sortValue || "") : "";
    };

    const renderRoster = () => {
        const rows = Array.from(rosterBody.querySelectorAll('tr'));
        const q = rosterFilter ? rosterFilter.value.toLowerCase() : "";
        const filtered = rows.filter((row) => {
            if (!q) return true;
            const nameCell = row.querySelector('[data-sort-cell="name"]');
            const name = nameCell ? (nameCell.textContent || "") : "";
            return name.toLowerCase().indexOf(q) !== -1;
        });
        if (sortKey) {
            filtered.sort((a, b) => {
                let va = cellSortValue(a, sortKey);
                let vb = cellSortValue(b, sortKey);
                if (sortKey === "handicap") {
                    va = va === "" ? Infinity : Number(va);
                    vb = vb === "" ? Infinity : Number(vb);
                }
                let cmp = 0;
                if (va < vb) cmp = -1;
                else if (va > vb) cmp = 1;
                return cmp * sortDir;
            });
        }
        const fragment = document.createDocumentFragment();
        filtered.forEach((row) => fragment.appendChild(row));
        rosterBody.appendChild(fragment);
        const count = document.getElementById('roster-count');
        if (count && rosterFilter) {
            count.textContent = filtered.length + " of " + rows.length + " players";
        }
        updateSortIndicators();
    };

    const columnLabel = (key) => key.charAt(0).toUpperCase() + key.slice(1);

    const updateSortIndicators = () => {
        document.querySelectorAll('.roster__sort').forEach((link) => {
            const key = link.getAttribute('data-sort');
            const dirSpan = link.querySelector('.roster__sort-dir');
            if (key === sortKey) {
                link.setAttribute('aria-current', 'true');
                link.setAttribute('aria-label', 'Sort by ' + columnLabel(key) + ', ' + (sortDir === 1 ? 'ascending' : 'descending'));
                dirSpan.textContent = sortDir === 1 ? ' \u2191' : ' \u2193';
            } else {
                link.removeAttribute('aria-current');
                link.removeAttribute('aria-label');
                dirSpan.textContent = '';
            }
        });
    };

    document.querySelectorAll('.roster__sort').forEach((link) => {
        const dirSpan = document.createElement('span');
        dirSpan.className = 'roster__sort-dir';
        dirSpan.setAttribute('aria-hidden', 'true');
        link.appendChild(dirSpan);
        link.addEventListener('click', (event) => {
            event.preventDefault();
            const key = link.getAttribute('data-sort');
            if (key === sortKey) {
                sortDir = -sortDir;
            } else {
                sortKey = key;
                sortDir = 1;
            }
            renderRoster();
        });
    });

    if (rosterFilter) {
        rosterFilter.addEventListener('input', renderRoster);
    }

    // Detail dialog: fetch public roster detail for one golfer on demand. The
    // roster HTML never renders email/phone; they are admin-only and are never
    // returned by this JSON endpoint.
    rosterBody.addEventListener('click', (event) => {
        const button = event.target.closest('.roster__detail');
        if (!button) return;
        event.preventDefault();
        const golferId = button.getAttribute('data-golfer-id');
        rosterDialog.showModal();
        detailName.textContent = 'Loading…';
        detailFields.innerHTML = '';
        detailStatus.textContent = '';
        fetch('/roster/golfers/' + golferId, {
            headers: { Accept: 'application/json' },
            credentials: 'same-origin',
        })
            .then(async (response) => {
                if (!response.ok) throw new Error('Details unavailable');
                return response.json();
            })
            .then((data) => {
                if (!rosterDialog.open) return;
                detailName.textContent = data.name || 'Golfer';
                const dl = document.createElement('dl');
                dl.className = 'roster-dialog__details';
                const field = (label, value) => {
                    const dt = document.createElement('dt');
                    dt.textContent = label;
                    const dd = document.createElement('dd');
                    dd.textContent = (value !== null && value !== undefined && value !== '') ? String(value) : 'Not recorded';
                    dl.appendChild(dt);
                    dl.appendChild(dd);
                };
                field('Handicap', (data.handicap_strokes !== null && data.handicap_strokes !== undefined) ? String(data.handicap_strokes) : 'No handicap on file');
                field('Tee', data.tee_color || 'Not recorded');
                detailFields.appendChild(dl);
            })
            .catch((error) => {
                if (!rosterDialog.open) return;
                detailName.textContent = 'Details unavailable';
                detailStatus.textContent = error.message;
            });
    });

    rosterDialog.addEventListener('click', (event) => {
        if (event.target === rosterDialog) rosterDialog.close();
    });

    renderRoster();
});
